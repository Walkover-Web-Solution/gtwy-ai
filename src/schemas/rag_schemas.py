from pydantic import BaseModel, ConfigDict, Field, model_validator


class RAGQueryRequest(BaseModel):
    model_config = ConfigDict(extra="allow")

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
