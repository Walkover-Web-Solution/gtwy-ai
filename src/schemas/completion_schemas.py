from typing import Annotated, Any, Literal

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, model_validator

from ._validators import HTTP_URL_REGEX

MONGO_ID_PATTERN = r"^[0-9a-fA-F]{24}$"
MongoId = Annotated[str, Field(pattern=MONGO_ID_PATTERN)]


class WebhookCredModel(BaseModel):
    model_config = ConfigDict(extra="forbid")

    url: str = Field(pattern=HTTP_URL_REGEX)
    headers: dict[str, str] | None = None


class ResponseFormatModel(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: str | None = None
    cred: dict[str, Any] | None = None

    @model_validator(mode="after")
    def validate_webhook_cred(self) -> "ResponseFormatModel":
        if self.type == "webhook":
            if not isinstance(self.cred, dict):
                raise ValueError("response_format.cred is required when type='webhook'")
            WebhookCredModel.model_validate(self.cred)
        return self


class UserUrlItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["image", "audio", "video", "file"] | None = None
    url: str | None = Field(default=None, pattern=HTTP_URL_REGEX)
    source: str | None = None


class SettingsModel(BaseModel):
    model_config = ConfigDict(extra="allow")

    response_format: ResponseFormatModel | None = None


class ConfigurationModel(BaseModel):
    model_config = ConfigDict(extra="forbid")

    model: str | None = Field(default=None, min_length=1, strict=True)
    type: Literal["chat", "embedding", "fine-tune", "reasoning", "image"] | None = None
    prompt: Any | None = None
    input: Any | None = None
    temperature: Any | None = None
    max_tokens: Any | None = None
    top_p: Any | None = None
    frequency_penalty: Any | None = None
    presence_penalty: Any | None = None
    n: Any | None = None
    logprobs: Any | None = None
    log_probability: Any | None = None
    stop: Any | None = None
    additional_stop_sequences: Any | None = None
    stream: Any | None = None
    tools: Any | None = None
    tool_choice: Any | None = None
    parallel_tool_calls: Any | None = None
    fine_tune_model: Any | None = None
    reasoning: Any | None = None
    response_format: ResponseFormatModel | None = None
    response_type: Any | None = None
    response_suffix: Any | None = None
    response_count: Any | None = None
    best_response_count: Any | None = None
    echo_input: Any | None = None
    is_rich_text: Any | None = None
    RTLayer: Any | None = None
    webhook: Any | None = None
    creativity_level: Any | None = None
    token_selection_limit: Any | None = None
    novelty_penalty: Any | None = None
    repetition_penalty: Any | None = None
    probability_cutoff: Any | None = None
    size: Any | None = None
    image_size: Any | None = None
    number_of_images: Any | None = None
    aspect_ratio: Any | None = None
    dimensions: Any | None = None
    quality: Any | None = None
    style: Any | None = None
    language: Any | None = None
    smart_format: Any | None = None
    detect_language: Any | None = None
    diarize: Any | None = None
    filler_words: Any | None = None
    punctuate: Any | None = None
    numerals: Any | None = None
    detect_entities: Any | None = None
    model_option: Any | None = None
    service_tier: Any | None = None
    settings: SettingsModel | None = None


_SERVICE_NAMES = Literal[
    "openai", "anthropic", "groq", "open_router", "mistral",
    "gemini", "grok", "deepseek", "deepgram", "neev_cloud", "moonshot",
]


class CompletionRequest(BaseModel):
    model_config = ConfigDict(extra="allow")

    # Accepts "agent_id" or "bridge_id" from the request body; stored as agent_id.
    agent_id: str | None = Field(default=None, validation_alias=AliasChoices("agent_id", "bridge_id"))
    version_id: str | None = None
    configuration: ConfigurationModel | None = None
    service: _SERVICE_NAMES | None = None
    apikey: str | None = None
    response_type: Any | None = None
    template_id: str | None = None
    variables: dict[str, Any] | None = None
    variables_path: dict[str, Any] | None = None
    extra_tools: list[Any] | None = None
    built_in_tools: list[Any] | None = None
    guardrails: dict[str, Any] | None = None
    web_search_filters: list[str] | dict[str, Any] | None = None
    orchestrator_flag: bool | None = None
    environment: str | None = None
    wrapper_id: str | None = None
    auto_model_select: dict[str, Any] | None = None
    cache_on: bool | None = None
    user: str | None = None
    images: list[Any] | None = None
    files: list[Any] | None = None
    user_urls: list[UserUrlItem] | None = None
    thread_id: str | None = None
    sub_thread_id: str | None = None
    stream: bool | None = None
    is_stream: bool | None = None
    is_playground: bool | None = None
    is_rerun: bool | None = None

    @model_validator(mode="after")
    def require_bridge_identifier(self) -> "CompletionRequest":
        if not self.agent_id and not self.version_id:
            raise ValueError("Either agent_id, bridge_id, or version_id must be provided")
        return self
