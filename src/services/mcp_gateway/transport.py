from contextlib import asynccontextmanager

from mcp import ClientSession
from mcp.client.sse import sse_client
from mcp.client.streamable_http import streamablehttp_client

from globals import logger


@asynccontextmanager
async def open_mcp_session(url: str, headers: dict | None = None):
    headers = headers or {}
    try:
        async with streamablehttp_client(url, headers=headers) as transport:
            read, write = transport[0], transport[1]
            async with ClientSession(read, write) as session:
                await session.initialize()
                yield session
                return
    except Exception as http_err:
        logger.info(f"open_mcp_session: streamable-http failed for {url} ({http_err!s}); falling back to SSE")

    async with sse_client(url, headers=headers) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            yield session
