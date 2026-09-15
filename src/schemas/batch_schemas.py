"""Pydantic schemas for POST /api/v2/model/batch/chat/completion request body.

Subclasses CompletionRequest to inherit all validated fields, then tightens
batch-specific fields (batch, webhook required) and validates batch_variables
length against batch length.
"""

from typing import Any

from pydantic import Field, model_validator

from ._validators import HTTP_URL_REGEX
from .completion_schemas import CompletionRequest


class BatchChatCompletionRequest(CompletionRequest):
    # batch is required and non-empty.
    batch: list[str] = Field(min_length=1)

    # webhook is required for batch (optional in parent).
    webhook: str = Field(pattern=HTTP_URL_REGEX)

    # Optional per-item variable substitutions; length must match batch.
    batch_variables: list[dict[str, Any]] | None = None

    @model_validator(mode="after")
    def validate_batch_variables_length(self) -> "BatchChatCompletionRequest":
        if self.batch_variables is not None and len(self.batch_variables) != len(self.batch):
            raise ValueError(
                f"batch_variables length ({len(self.batch_variables)}) "
                f"must match batch length ({len(self.batch)})"
            )
        return self
