import asyncio
from contextlib import AsyncExitStack, asynccontextmanager

import httpx
from mcp import ClientSession
from mcp.client.sse import sse_client
try:
    from mcp.client.streamable_http import streamablehttp_client
except ImportError:  # renamed in mcp 2.0
    from mcp.client.streamable_http import streamable_http_client as streamablehttp_client

from globals import logger


def outer_task_cancelled() -> bool:
    """True when the current task itself is being cancelled (e.g. the request
    was aborted), as opposed to the MCP transport cancelling its own work."""
    task = asyncio.current_task()
    return bool(task and task.cancelling())


def _leaf_errors(error: BaseException) -> list[BaseException]:
    if isinstance(error, BaseExceptionGroup):
        return [leaf for inner in error.exceptions for leaf in _leaf_errors(inner)]
    return [error]


def describe_mcp_error(error: BaseException) -> str:
    """Readable message for an MCP failure; transport errors arrive wrapped in
    (Base)ExceptionGroups whose str() says nothing useful."""
    leaves = [e for e in _leaf_errors(error) if not isinstance(e, asyncio.CancelledError)] or [error]
    for leaf in leaves:
        if isinstance(leaf, httpx.HTTPStatusError):
            status = leaf.response.status_code
            message = f"MCP server returned {status} {leaf.response.reason_phrase}"
            if status in (401, 403):
                message += "; check the auth configured for this MCP server"
            return message
    return str(leaves[0]) or type(leaves[0]).__name__


async def _connect(stack: AsyncExitStack, transport_cm) -> ClientSession:
    streams = await stack.enter_async_context(transport_cm)
    session = await stack.enter_async_context(ClientSession(streams[0], streams[1]))
    await session.initialize()
    return session


async def _try_connect(transport_cm) -> tuple[ClientSession, AsyncExitStack]:
    stack = AsyncExitStack()
    try:
        return await _connect(stack, transport_cm), stack
    except BaseException as connect_error:
        # Closing the transport surfaces the real error (e.g. an HTTP 401) that
        # cancelled the connect; prefer it over the bare CancelledError.
        real_error = connect_error
        try:
            await stack.aclose()
        except BaseException as close_error:
            real_error = close_error
        if outer_task_cancelled() or isinstance(real_error, KeyboardInterrupt | SystemExit):
            raise
        raise real_error from None


@asynccontextmanager
async def open_mcp_session(url: str, headers: dict | None = None):
    """Open an initialized MCP session, trying streamable HTTP first and SSE
    second. The fallback only covers connecting; an error while the session is
    in use (e.g. a tool call rejected with 401) is raised to the caller."""
    headers = headers or {}
    try:
        session, stack = await _try_connect(streamablehttp_client(url, headers=headers))
    except BaseException as http_err:
        if outer_task_cancelled() or isinstance(http_err, KeyboardInterrupt | SystemExit):
            raise
        logger.info(
            f"open_mcp_session: streamable-http failed for {url} ({describe_mcp_error(http_err)}); falling back to SSE"
        )
        session, stack = await _try_connect(sse_client(url, headers=headers))

    async with stack:
        yield session
