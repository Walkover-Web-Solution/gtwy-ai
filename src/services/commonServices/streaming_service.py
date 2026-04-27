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

    async def emit_template_response(self, message_id: str, content: dict, metadata: dict | None = None):
        payload = {
            "event": "template_response",
            "message_id": message_id,
            "content": content,
        }
        if metadata:
            payload["metadata"] = metadata
        await self._emit(payload)

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

    async def emit_task_delta(self, task_id: str, content: str):
        await self._emit({"event": "task_delta", "task_id": task_id, "content": content})

    async def emit_task_reasoning(self, task_id: str, content: str):
        await self._emit({"event": "task_reasoning", "task_id": task_id, "content": content})

    async def emit_task_tool_call(self, task_id: str, name: str, args: dict, call_id: str):
        await self._emit({"event": "task_tool_call", "task_id": task_id, "name": name, "args": args, "call_id": call_id})

    async def emit_task_tool_result(self, task_id: str, name: str, content: str, call_id: str):
        await self._emit({"event": "task_tool_result", "task_id": task_id, "name": name, "content": content, "call_id": call_id})

    async def emit_planning(self):
        """Emit a planning mode event."""
        await self._emit({"event": "planning"})

    async def emit_review_phase(self, phase: str, round: int, **kwargs):
        """
        Emit a review-loop phase marker so frontends can display what the
        backend is doing between rounds (reviewing vs revising).

        phase ∈ {"reviewer_start", "reviewer_done", "main_rerun_start"}
        Optional kwargs flow into the payload (e.g. passed=bool, reason=str).
        """
        payload = {"event": "review_phase", "phase": phase, "round": round}
        payload.update(kwargs)
        await self._emit(payload)

    async def emit_execution(self):
        """Emit an execution mode event."""
        await self._emit({"event": "execution"})

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
