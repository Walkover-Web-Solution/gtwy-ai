import httpx

from config import Config

DOWNLOAD_TIMEOUT_SECONDS = 30


class FileTooLargeError(Exception):
    pass


async def download_file(url: str, max_bytes: int | None = None) -> tuple[bytes, str | None]:
    """Download ``url`` and return (bytes, content_type), stopping at ``max_bytes``.

    Streams the body so an oversized file is refused without holding it all in
    memory (``apiservice.fetch`` has no size cap).
    """
    limit = max_bytes or Config.FILE_BRIDGE_MAX_BYTES
    if not url.lower().startswith(("http://", "https://")):
        raise ValueError("only http(s) URLs can be read")

    async with httpx.AsyncClient(timeout=DOWNLOAD_TIMEOUT_SECONDS, follow_redirects=True) as client:
        async with client.stream("GET", url) as response:
            response.raise_for_status()
            declared = response.headers.get("content-length")
            if declared and declared.isdigit() and int(declared) > limit:
                raise FileTooLargeError(f"file is {int(declared)} bytes, limit is {limit}")
            chunks = []
            size = 0
            async for chunk in response.aiter_bytes():
                size += len(chunk)
                if size > limit:
                    raise FileTooLargeError(f"file is over the {limit} byte limit")
                chunks.append(chunk)
            return b"".join(chunks), response.headers.get("content-type")
