"""Per-provider adapters for the provider Files APIs.

Each adapter knows how to upload/delete a file for its provider and how to
render the file reference as a message content part. Providers without an
adapter keep the existing URL pass-through behavior.
"""

from abc import ABC, abstractmethod

from anthropic import AsyncAnthropic
from openai import AsyncOpenAI

from globals import logger
from src.configs.constant import service_name


class DeleteResult:
    OK = "ok"                 # deleted on provider
    ALREADY_GONE = "gone"     # 404 — nothing left to delete
    AUTH_FAILED = "auth"      # 401/403 — key rotated/revoked, retrying is pointless
    RETRYABLE = "retry"       # 429/5xx/network — try again next pass


def _map_status(status_code: int | None) -> str:
    if status_code == 404:
        return DeleteResult.ALREADY_GONE
    if status_code in (401, 403):
        return DeleteResult.AUTH_FAILED
    return DeleteResult.RETRYABLE


class ProviderFileAdapter(ABC):
    provider: str
    max_size_bytes: int
    upload_purpose: str = ""

    @abstractmethod
    async def upload(self, apikey: str, filename: str, data: bytes, mime_type: str) -> str:
        """Upload bytes, return the provider file_id. Raises on failure."""

    @abstractmethod
    async def delete(self, apikey: str, file_id: str) -> str:
        """Delete the provider file. Returns a DeleteResult constant, never raises."""

    @abstractmethod
    def build_message_part(self, file_id: str, mime_type: str) -> dict:
        """Render the file reference as a message content part for this provider."""

    async def retrieve(self, apikey: str, file_id: str) -> dict | None:
        """Fetch live metadata for file_id directly from the provider.

        Returns {"exists": bool, "status": ..., "bytes": ...} or None if this
        provider doesn't support it (default: unsupported)."""
        return None


class OpenAIFileAdapter(ProviderFileAdapter):
    provider = service_name["openai"]
    max_size_bytes = 512 * 1024 * 1024
    upload_purpose = "user_data"

    async def upload(self, apikey, filename, data, mime_type):
        client = AsyncOpenAI(api_key=apikey)
        result = await client.files.create(file=(filename, data, mime_type), purpose=self.upload_purpose)
        return result.id

    async def delete(self, apikey, file_id):
        try:
            client = AsyncOpenAI(api_key=apikey)
            await client.files.delete(file_id)
            return DeleteResult.OK
        except Exception as e:
            outcome = _map_status(getattr(e, "status_code", None))
            if outcome == DeleteResult.RETRYABLE:
                logger.error(f"OpenAI file delete failed for {file_id}: {e}")
            return outcome

    def build_message_part(self, file_id, mime_type):
        return {"type": "input_file", "file_id": file_id}

    async def retrieve(self, apikey, file_id):
        try:
            client = AsyncOpenAI(api_key=apikey)
            f = await client.files.retrieve(file_id)
            return {
                "exists": True,
                "status": getattr(f, "status", None),
                "bytes": getattr(f, "bytes", None),
                "filename": getattr(f, "filename", None),
            }
        except Exception as e:
            status_code = getattr(e, "status_code", None)
            if status_code == 404:
                return {"exists": False, "status": None, "bytes": None, "filename": None}
            logger.error(f"OpenAI file retrieve failed for {file_id}: {e}")
            return {"exists": None, "status": None, "bytes": None, "filename": None, "error": str(e)}


class AnthropicFileAdapter(ProviderFileAdapter):
    provider = service_name["anthropic"]
    max_size_bytes = 500 * 1024 * 1024
    _beta_headers = {"anthropic-beta": "files-api-2025-04-14"}

    async def upload(self, apikey, filename, data, mime_type):
        client = AsyncAnthropic(api_key=apikey)
        result = await client.beta.files.upload(
            file=(filename, data, mime_type), extra_headers=self._beta_headers
        )
        return result.id

    async def delete(self, apikey, file_id):
        try:
            client = AsyncAnthropic(api_key=apikey)
            await client.beta.files.delete(file_id, extra_headers=self._beta_headers)
            return DeleteResult.OK
        except Exception as e:
            outcome = _map_status(getattr(e, "status_code", None))
            if outcome == DeleteResult.RETRYABLE:
                logger.error(f"Anthropic file delete failed for {file_id}: {e}")
            return outcome

    def build_message_part(self, file_id, mime_type):
        if mime_type and mime_type.startswith("image/"):
            return {"type": "image", "source": {"type": "file", "file_id": file_id}}
        return {"type": "document", "source": {"type": "file", "file_id": file_id}}


FILE_ADAPTERS: dict[str, ProviderFileAdapter] = {
    adapter.provider: adapter
    for adapter in (OpenAIFileAdapter(), AnthropicFileAdapter())
}


def get_file_adapter(provider: str) -> ProviderFileAdapter | None:
    return FILE_ADAPTERS.get(provider)
