from dataclasses import dataclass, field


@dataclass
class FileBridgeContext:
    """What a conversion needs from the request: keys to pay with and where to log cost."""

    org_id: str | None = None
    service_apikeys: dict = field(default_factory=dict)
    # Shared with parsed_data["file_bridge_usage"], so helper calls made by a
    # failed first attempt, a fallback, or the read_file tool all get billed.
    usage_log: list = field(default_factory=list)
    # URLs the read_file tool may open: this thread's attachments only.
    allowed_urls: set = field(default_factory=set)
    # url -> kind, so the tool does not have to guess again.
    url_kinds: dict = field(default_factory=dict)
