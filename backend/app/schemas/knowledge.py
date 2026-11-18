"""Knowledge Document and Chunk Pydantic schemas."""
from datetime import datetime
from pydantic import BaseModel, ConfigDict


class KnowledgeDocBase(BaseModel):
    title: str
    filename: str
    file_type: str = "txt"
    document_type: str = "manual"
    product_id: int | None = None


class KnowledgeDocCreate(KnowledgeDocBase):
    content: str | None = None


class KnowledgeDocOut(KnowledgeDocBase):
    model_config = ConfigDict(from_attributes=True)

    id: int
    file_path: str
    file_size_bytes: int
    is_indexed: bool
    chunk_count: int
    error_message: str | None = None
    created_at: datetime
    updated_at: datetime


class KnowledgeChunkOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    document_id: int
    chunk_index: int
    content: str
    metadata_json: str | None = None
    has_embedding: bool = False
    created_at: datetime


class SearchQuery(BaseModel):
    query: str
    product_id: int | None = None
    top_k: int = 4
    min_similarity: float = 0.5


class SearchResult(BaseModel):
    chunk_id: int
    document_id: int
    document_title: str
    filename: str
    content: str
    similarity: float
    citation: str
    retrieval_method: str = "semantic"
