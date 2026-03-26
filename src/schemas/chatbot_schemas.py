from pydantic import BaseModel, ConfigDict, Field, model_validator


class ChatbotSendMessageRequest(BaseModel):
    model_config = ConfigDict(extra="allow")

    slugName: str
    message: str | None = None
    threadId: str | None = None
    subThreadId: str | None = None
    images: list = Field(default_factory=list)
    flag: bool = False
    variables: dict = Field(default_factory=dict)
    interfaceContextData: dict = Field(default_factory=dict)
    configuration: dict = Field(default_factory=dict)

    @model_validator(mode="after")
    def check_message_or_images(self) -> "ChatbotSendMessageRequest":
        if not (self.message or "").strip() and not self.images:
            raise ValueError("Either message or images must be provided")
        return self
