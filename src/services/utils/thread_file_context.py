"""Decide which document(s) in a thread are the *working* files for this turn.

Rules (see openai_response.py):
  * The most recent user upload is the *original*. A newer upload replaces any
    earlier one — earlier uploads (and edits derived from them) are superseded
    and no longer sent to the model.
  * The most recent file the assistant generated after that upload (llm_urls
    entries of type "file", e.g. code_interpreter outputs) is the *latest*
    version. Further edit requests are applied to it, while the original stays
    available so the user can ask to start over.
  * Uploading a new file in the current turn resets both.
"""

from dataclasses import dataclass, field


@dataclass
class ThreadFileContext:
    original_urls: list[str] = field(default_factory=list)  # current upload(s)
    latest_url: str | None = None  # newest assistant-generated version, if any
    latest_filename: str | None = None
    superseded_urls: set[str] = field(default_factory=set)  # older uploads/versions to drop

    @property
    def active_urls(self) -> list[str]:
        urls = list(self.original_urls)
        if self.latest_url and self.latest_url not in urls:
            urls.append(self.latest_url)
        return urls


def _message_file_urls(message: dict) -> list[str]:
    return [
        entry.get("url")
        for entry in (message.get("user_urls") or [])
        if isinstance(entry, dict) and entry.get("url") and entry.get("type") != "image"
    ]


def _generated_files(message: dict) -> list[dict]:
    return [
        entry
        for entry in (message.get("llm_urls") or [])
        if isinstance(entry, dict) and entry.get("type") == "file" and entry.get("permanent_url")
    ]


def compute_thread_file_context(conversation: list[dict] | None, current_files: list[str] | None) -> ThreadFileContext:
    ctx = ThreadFileContext()
    all_seen: list[str] = []

    for message in conversation or []:
        role = message.get("role")
        if role == "user":
            uploads = _message_file_urls(message)
            if uploads:
                all_seen.extend(uploads)
                ctx.original_urls = uploads
                ctx.latest_url = None
                ctx.latest_filename = None
        elif role == "assistant":
            generated = _generated_files(message)
            if generated:
                for f in generated:
                    all_seen.append(f["permanent_url"])
                ctx.latest_url = generated[-1]["permanent_url"]
                ctx.latest_filename = generated[-1].get("filename")

    if current_files:
        # a new upload this turn replaces everything that came before
        ctx.original_urls = list(current_files)
        ctx.latest_url = None
        ctx.latest_filename = None

    active = set(ctx.active_urls)
    ctx.superseded_urls = {url for url in all_seen if url not in active}
    return ctx
