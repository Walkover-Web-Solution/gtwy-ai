"""Pydantic schemas for POST /api/v2/model/batch/chat/completion request body.

Subclasses CompletionRequest to inherit all validated fields, then tightens
batch-specific fields (batch, webhook required) and validates batch_variables
length against batch length.
"""

from typing import Any

from pydantic import Field, field_validator, model_validator

from .completion_schemas import CompletionRequest, WebhookCredModel


class BatchChatCompletionRequest(CompletionRequest):
    # batch is required and non-empty.
    batch: list[str] = Field(min_length=1)

    # webhook is required for batch (optional in parent): { url, headers? }.
    # url must match HTTP_URL_REGEX (enforced via WebhookCredModel).
    webhook: dict[str, Any]

    # Optional per-item variable substitutions; length must match batch.
    batch_variables: list[dict[str, Any]] | None = None

    @field_validator("webhook")
    @classmethod
    def validate_webhook(cls, value: dict[str, Any]) -> dict[str, Any]:
        WebhookCredModel.model_validate(value)
        return value

    @model_validator(mode="after")
    def validate_batch_variables_length(self) -> "BatchChatCompletionRequest":
        if self.batch_variables is not None and len(self.batch_variables) != len(self.batch):
            raise ValueError(
                f"batch_variables length ({len(self.batch_variables)}) "
                f"must match batch length ({len(self.batch)})"
            )
        return self
