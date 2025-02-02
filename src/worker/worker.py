import sys
import os

# this is needed to import classes from other directories
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '../')))

import re
import ssl
import time
import pika
import json
import openai
import requests 
import logging
import worker.config as config
import services.database.batch_service as batch_service
import services.database.job_service as job_service
from shared.chunk_strategy import ChunkStrategy
from shared.embeddings_type import EmbeddingsType
from shared.batch_status import BatchStatus
from concurrent.futures import ThreadPoolExecutor, as_completed
from services.database.database import get_db
from shared.job_status import JobStatus

logging.basicConfig(filename='./worker-log.txt', level=logging.INFO)
logging.basicConfig(filename='./worker-errors.txt', level=logging.ERROR)
publish_channel = None
connection = None

def process_batch(batch_id, source_data):
    with get_db() as db:
        batch = batch_service.get_batch(db, batch_id)
        job = job_service.get_job(db, batch.job_id)

        # TODO: update this logic once the batch creation logic is moved out of the API
        if job.job_status == JobStatus.NOT_STARTED or job.job_status == JobStatus.CREATING_BATCHES:
            job_service.update_job_status(db, job.id, JobStatus.PROCESSING_BATCHES)
        
        if batch.batch_status == BatchStatus.NOT_STARTED:
            batch_service.update_batch_status(db, batch.id, BatchStatus.PROCESSING)
        else:
            batch_service.update_batch_retry_count(db, batch.id, batch.retries+1)
            logging.info(f"Retrying batch {batch.id}")

        db.refresh(batch)

        embeddings_type = batch.embeddings_metadata.embeddings_type
        if embeddings_type == EmbeddingsType.OPEN_AI:
            try:
                text_embeddings_list = embed_openai_batch(batch, source_data)
                if text_embeddings_list:
                    upload_to_vector_db(batch_id, text_embeddings_list)
                else:
                    logging.error(f"Failed to get OPEN AI embeddings for batch {batch.id}. Adding batch to retry queue.")
                    update_batch_status(batch.job_id, BatchStatus.FAILED, batch.id)

            except Exception as e:
                logging.error('Error embedding batch:', e)
                update_batch_status(batch.job_id, BatchStatus.FAILED, batch.id)
        elif embeddings_type == EmbeddingsType.HUGGING_FACE:
            try:
                embed_hugging_face_batch(batch, source_data)
            except Exception as e:
                logging.error('Error embedding batch:', e)
                update_batch_status(batch.job_id, BatchStatus.FAILED, batch.id)
        else:
            logging.error('Unsupported embeddings type:', embeddings_type)
            update_batch_status(batch.job_id, BatchStatus.FAILED, batch.id)

# NOTE: this method will embed mulitple chunks (a list of strings) at once and return a list of lists of floats (a list of embeddings)
def get_openai_embedding(batch_of_chunks, attempts=5):
    for i in range(attempts):
        try:
            response = openai.Embedding.create(
                model= "text-embedding-ada-002",
                input=batch_of_chunks
            )
            if response["data"]:
                return batch_of_chunks, response["data"]
        except Exception as e:
            logging.error('Open AI Embedding API call failed:', e)
            time.sleep(2**i)  # Exponential backoff: 1, 2, 4, 8, 16 seconds.
    return batch_of_chunks, None

def embed_openai_batch(batch, source_data):
    logging.info("Starting Open AI Embeddings")
    openai.api_key = os.getenv('EMBEDDING_API_KEY')
    
    chunked_data = chunk_data(batch.embeddings_metadata.chunk_strategy, source_data, batch.embeddings_metadata.chunk_size, batch.embeddings_metadata.chunk_overlap)
    
    # Maximum number of items allowed in a batch by OpenAIs embedding API. There is also an 8191 token per item limit
    open_ai_batches = create_upload_batches(chunked_data, max_batch_size=config.MAX_OPENAI_EMBEDDING_BATCH_SIZE)
    text_embeddings_list = list()

    with ThreadPoolExecutor(max_workers=config.MAX_THREADS_OPENAI) as executor:
        futures = [executor.submit(get_openai_embedding, chunk) for chunk in open_ai_batches]
        for future in as_completed(futures):
            chunks, embeddings = future.result()
            if embeddings is not None:
                for text, embedding in zip(chunks, embeddings):
                    text_embeddings_list.append((text, embedding['embedding']))
            else:
                logging.error(f"Failed to get embedding for chunk {chunks}. Adding batch to retry queue.")
                update_batch_status(batch.job_id, BatchStatus.Failed, batch.id)
                return
    
    logging.info("Open AI Embeddings completed successfully")
    return text_embeddings_list

def publish_to_embedding_queue(batch_id, batch_of_chunks, model_name, attempts=5):
    for _ in range(attempts):
        try:
            embedding_channel = connection.channel() 
            embedding_channel.queue_declare(queue=model_name)
            serialized_data = json.dumps((batch_id, batch_of_chunks, os.getenv('VECTOR_DB_KEY')))
            embedding_channel.basic_publish(exchange='',
                                        routing_key=model_name,
                                        body=serialized_data)
            logging.info(f"Message published to open source queue {model_name} successfully")
            return
        except Exception as e:
            logging.error('ERROR connecting to RabbitMQ, retrying now. See exception:', e)
            time.sleep(config.PIKA_RETRY_INTERVAL)
    
    # TODO: implement logic to handle partial failures & retries
    with get_db() as db:
        batch_service.update_batch_status(db, batch_id, BatchStatus.FAILED)
        logging.error(f"Failed to publish batch {batch_id} to open source queue {model_name} after {attempts} attempts.")

def embed_hugging_face_batch(batch, source_data):
    logging.info(f"Starting Hugging Face Embeddings with {batch.embeddings_metadata.hugging_face_model_name}")
    
    chunked_data = chunk_data(batch.embeddings_metadata.chunk_strategy, source_data, batch.embeddings_metadata.chunk_size, batch.embeddings_metadata.chunk_overlap)
    hugging_face_batches = create_upload_batches(chunked_data, config.HUGGING_FACE_BATCH_SIZE)
    
    with get_db() as db:
        batch_service.update_batch_minibatch_count(db, batch.id, len(hugging_face_batches))
    
    for batch_of_chunks in hugging_face_batches:
        publish_to_embedding_queue(batch.id, batch_of_chunks, batch.embeddings_metadata.hugging_face_model_name)

def chunk_data(chunk_strategy, source_data, chunk_size, chunk_overlap):
    if chunk_strategy == ChunkStrategy.EXACT:
        chunked_data = chunk_data_exact(source_data, chunk_size, chunk_overlap)

    elif chunk_strategy == ChunkStrategy.PARAGRAPH:
        chunked_data = chunk_data_by_paragraph(source_data,chunk_size, chunk_overlap)

    elif chunk_strategy == ChunkStrategy.SENTENCE:
        chunked_data = chunk_by_sentence(source_data, chunk_size, chunk_overlap)
    else:
        chunked_data = chunk_data_exact(source_data, chunk_size, chunk_overlap)
    return chunked_data

def chunk_data_exact(data_chunks, chunk_size, chunk_overlap):
    data = "".join(data_chunks)
    chunks = []
    for i in range(0, len(data), chunk_size - chunk_overlap):
        chunks.append(data[i:i + chunk_size])
    return chunks

def chunk_data_by_paragraph(data_chunks, chunk_size, overlap, bound=0.75):
    data = "".join(data_chunks)
    total_length = len(data)
    chunks = []
    check_bound = int(bound * chunk_size)
    start_idx = 0

    while start_idx < total_length:
        # Set the end index to the minimum of start_idx + default_chunk_size or total_length
        end_idx = min(start_idx + chunk_size, total_length)

        # Find the next paragraph index within the current chunk and bound
        next_paragraph_index = data.find('\n\n', start_idx + check_bound, end_idx)

        # If a next paragraph index is found within the current chunk
        if next_paragraph_index != -1:
            # Update end_idx to include the paragraph delimiter
            end_idx = next_paragraph_index + 2

        chunks.append(data[start_idx:end_idx + overlap])

        # Update start_idx to be the current end_idx
        start_idx = end_idx

    return chunks

def chunk_by_sentence(data_chunks, chunk_size, overlap):
    # Split by periods, question marks, exclamation marks, and ellipses
    data = "".join(data_chunks)

    # The regular expression is used to find series of charaters that end with one the following chaacters (. ! ? ...)
    sentence_endings = r'(?<=[.!?…]) +'
    sentences = re.split(sentence_endings, data)
    
    sentence_chunks = []
    for sentence in sentences:
        if len(sentence) > chunk_size:
            chunks = chunk_data_exact([sentence], chunk_size, overlap)
            sentence_chunks.extend(chunks)
        else:
            sentence_chunks.append(sentence)
    return sentence_chunks

def create_upload_batches(batches, max_batch_size):
    open_ai_batches = [batches[i:i + max_batch_size] for i in range(0, len(batches), max_batch_size)]
    return open_ai_batches

def update_batch_status(job_id, batch_status, batch_id):
    try:
        with get_db() as db:
            updated_batch_status = batch_service.update_batch_status(db, batch_id, batch_status)
            logging.info(f"Batch {batch_id} for job {job_id} status updated to {updated_batch_status}")      
    except Exception as e:
        logging.error('Error updating batch status:', e)

def upload_to_vector_db(batch_id, text_embeddings_list):
    try:
        serialized_data = json.dumps((batch_id, text_embeddings_list, os.getenv('VECTOR_DB_KEY')))
        publish_channel.basic_publish(exchange='',
                                      routing_key=os.getenv('VDB_UPLOAD_QUEUE'),
                                      body=serialized_data)
        logging.info("Message published successfully")
    except Exception as e:
        logging.error('Error publishing message to RabbitMQ:', e)

def callback(ch, method, properties, body):
    try:
        data = json.loads(body)
        batch_id, source_data, vector_db_key, embeddings_api_key = data
        os.environ["VECTOR_DB_KEY"] = vector_db_key
        os.environ["EMBEDDING_API_KEY"] = embeddings_api_key

        logging.info("Batch retrieved successfully")
        process_batch(batch_id, source_data)
        logging.info("Batch processed successfully")
    except Exception as e:
        logging.error('Error processing batch:', e)

    ch.basic_ack(delivery_tag=method.delivery_tag)

def start_connection():
    global publish_channel
    global connection
    
    while True:
        try:
            credentials = pika.PlainCredentials(os.getenv('RABBITMQ_USERNAME'), os.getenv('RABBITMQ_PASSWORD'))

            connection_params = pika.ConnectionParameters(
                host=os.getenv('RABBITMQ_HOST'),
                credentials=credentials,
                port=os.getenv('RABBITMQ_PORT'),
                heartbeat=600,
                ssl_options=pika.SSLOptions(ssl.SSLContext(ssl.PROTOCOL_TLSv1_2)),
                virtual_host="/"
            ) if os.getenv('RABBITMQ_PORT') == "5671" else pika.ConnectionParameters(
                host=os.getenv('RABBITMQ_HOST'),
                credentials=credentials,
                heartbeat=600,
            )

            connection = pika.BlockingConnection(connection_params)
            consume_channel = connection.channel()
            publish_channel = connection.channel() 

            consume_queue_name = os.getenv('EMBEDDING_QUEUE')
            publish_queue_name = os.getenv('VDB_UPLOAD_QUEUE')

            consume_channel.queue_declare(queue=consume_queue_name)
            publish_channel.queue_declare(queue=publish_queue_name)

            consume_channel.basic_consume(queue=consume_queue_name, on_message_callback=callback)

            logging.info('Waiting for messages.')
            consume_channel.start_consuming()
            
        except Exception as e:
            logging.error('ERROR connecting to RabbitMQ, retrying now. See exception:', e)
            time.sleep(config.PIKA_RETRY_INTERVAL) # Wait before retrying

if __name__ == "__main__":
    start_connection()
