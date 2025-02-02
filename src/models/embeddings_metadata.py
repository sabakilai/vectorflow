import json
from sqlalchemy import Column, Integer, String, Enum
from services.database.database import Base
from shared.embeddings_type import EmbeddingsType
from shared.chunk_strategy import ChunkStrategy

class EmbeddingsMetadata(Base):
    __tablename__ = 'embeddings_metadata'

    id = Column(Integer, primary_key=True)
    embeddings_type = Column(Enum(EmbeddingsType))
    chunk_size = Column(Integer)
    chunk_overlap = Column(Integer)
    chunk_strategy = Column(Enum(ChunkStrategy))
    docker_image = Column(String)
    hugging_face_model_name = Column(String)

    def serialize(self):
        return {
            'embeddings_type': self.embeddings_type.name if self.embeddings_type else None,
            'chunk_size': self.chunk_size,
            'chunk_overlap': self.chunk_overlap,
            'chunk_strategy': self.chunk_strategy,
            'docker_image': self.docker_image,
            'hugging_face_model_name': self.hugging_face_model_name
        }
    
    @staticmethod
    def _from_request(request):
        embeddings_metadata_dict = json.loads(request.form.get('EmbeddingsMetadata'))
        embeddings_metadata = EmbeddingsMetadata(
            embeddings_type = EmbeddingsType[embeddings_metadata_dict['embeddings_type']], 
            chunk_size = embeddings_metadata_dict['chunk_size'],
            chunk_overlap = embeddings_metadata_dict['chunk_overlap'],
            chunk_strategy = embeddings_metadata_dict['chunk_strategy'] if 'chunk_strategy' in embeddings_metadata_dict else ChunkStrategy.EXACT,
            docker_image = embeddings_metadata_dict['docker_image'] if 'docker_image' in embeddings_metadata_dict else None,
            hugging_face_model_name = embeddings_metadata_dict['hugging_face_model_name'] if 'hugging_face_model_name' in embeddings_metadata_dict else None)
        return embeddings_metadata
