from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, field_validator, model_validator


class RAGQueryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str
    top_k: int = Field(default=2, ge=1)
    score: float = Field(default=0.1, ge=0.1, le=1.0)
    collection_id: str | None = None
    owner_id: str | None = None
    doc_id: str | None = None
    resource_id: str | None = None

    @model_validator(mode="after")
    def check_resource_or_collection(self) -> "RAGQueryRequest":
        has_collection = bool(self.collection_id and self.owner_id)
        has_resource = bool(self.doc_id or self.resource_id)
        if not (has_collection or has_resource):
            raise ValueError("Either (collection_id and owner_id) or (doc_id / resource_id) must be provided.")
        return self


class RAGCreateFormRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    name: str = Field(min_length=1)
    description: str = Field(min_length=1)
    chunking_type: Literal["recursive", "manual", "semantic"] = "recursive"
    chunk_size: int = Field(default=512, gt=0)
    chunk_overlap: int | None = Field(default=None, ge=0)
    doc_url: str | None = None

    @field_validator("chunk_overlap", mode="before")
    @classmethod
    def set_chunk_overlap_default(cls, v: int | None, info: ValidationInfo) -> int:
        if v is None:
            chunk_size = info.data.get("chunk_size", 512)
            return int(chunk_size * 0.15)
        return v


class RAGDeleteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1)
