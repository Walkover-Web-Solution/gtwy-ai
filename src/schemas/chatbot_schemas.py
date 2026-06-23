from pydantic import Field, model_validator

from .completion_schemas import CompletionRequest


class ChatbotSendMessageRequest(CompletionRequest):
    slugName: str 
    chatBotId: str | None = None
    userId: int | None = None
    message: str | None = None
    threadId: str | None = None
    subThreadId: str | None = None
    thread_flag: bool | None = None
    images: list = Field(default_factory=list)
    flag: bool = False
    variables: dict = Field(default_factory=dict)
    interfaceContextData: dict = Field(default_factory=dict)

    @model_validator(mode="after")
    def require_bridge_identifier(self) -> "ChatbotSendMessageRequest":
        # Chatbots use slugName as the identifier, so we bypass the parent's check for bridge_id/agent_id
        return self

    @model_validator(mode="after")
    def require_user_content(self) -> "ChatbotSendMessageRequest":
        if not (self.message or "").strip() and not self.images:
            raise ValueError("Either message or images must be provided")
        return self
