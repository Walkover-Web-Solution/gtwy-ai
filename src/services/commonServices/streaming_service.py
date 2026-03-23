import asyncio
import json

from .baseService.utils import send_message


class StreamingService:
    """
    GTWY's own streaming abstraction.

    Two delivery modes:
    - "sse"      : chunks go onto an asyncio.Queue, drained by StreamingResponse
    - "rtlayer"  : each event is POSTed to the RTLayer channel immediately via send_message()
    """

    def __init__(self, mode: str = "sse", rtlayer_cred: dict = None):
        self.mode = mode
        self.rtlayer_cred = rtlayer_cred or {}
        self.queue = asyncio.Queue() if mode == "sse" else None
        self._started = False

    async def _emit(self, payload: dict):
        if self.mode == "sse":
            await self.queue.put(f"data: {json.dumps(payload)}\n\n")
        elif self.mode == "rtlayer":
            channel = self.rtlayer_cred.get("channel", "unknown")
            print(f"[RTLayer] sending event='{payload.get('event')}' to channel='{channel}'")
            await send_message(cred=self.rtlayer_cred, data=payload)
            

    async def emit_start(self, model: str, service: str, bridge_id: str, message_id: str):
        if self._started:
            return
        self._started = True
        await self._emit({
            "event": "start",
            "model": model,
            "service": service,
            "bridge_id": bridge_id,
            "message_id": message_id,
        })

    async def emit_delta(self, content: str):
        await self._emit({"event": "delta", "content": content})

    async def emit_reasoning(self, content: str):
        await self._emit({"event": "reasoning", "content": content})

    async def emit_tool_call(self, name: str, args: dict, call_id: str):
        await self._emit({"event": "tool_call", "name": name, "args": args, "call_id": call_id})

    async def emit_tool_result(self, name: str, content: str, call_id: str):
        await self._emit({"event": "tool_result", "name": name, "content": content, "call_id": call_id})

    async def emit_done(self, usage: dict, message_id: str, finish_reason: str, accumulated_data: dict = None):
        payload = {
            "event": "done",
            "usage": usage,
            "message_id": message_id,
            "finish_reason": finish_reason,
        }
        if accumulated_data is not None:
            payload["response"] = accumulated_data
        await self._emit(payload)

    async def emit_error(self, error: str, fallback_error: str = None):
        payload = {"event": "error", "error": error}
        if fallback_error is not None:
            payload["fallback_error"] = fallback_error
        await self._emit(payload)

    async def generator(self):
        """SSE mode only — async generator that drains the queue for StreamingResponse."""
        while True:
            chunk = await self.queue.get()
            if chunk is None:
                break
            yield chunk

    async def close(self):
        if self.mode == "sse" and self.queue is not None:
            await self.queue.put(None)
