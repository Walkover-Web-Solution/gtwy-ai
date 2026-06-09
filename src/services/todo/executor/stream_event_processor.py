import json

from src.services.todo.executor.tools import inject_variables_into_tool_args


async def process_worker_stream(
    body_iterator,
    *,
    task_id: str,
    aggregate_metrics: dict | None,
    streamer,
    variables: dict,
    variables_path: dict,
    assigned_agent: str,
    bridge_configurations: dict,
    forward_deltas: bool = True,
    forward_reasoning: bool = True,
) -> tuple[str, dict | None, list, list]:
    """Consume SSE events from a streaming worker response.

    Forwards delta/reasoning/tool events to the streamer in real-time and
    accumulates telemetry into aggregate_metrics when provided.

    `forward_deltas` / `forward_reasoning` let callers suppress per-token
    forwarding (e.g. A2A, where the deltas are the internal JSON envelope
    and shouldn't be shown to the user). Content is still accumulated and
    returned so the caller can parse the envelope server-side. Tool events
    are always forwarded so the FE can show which tools the agent ran.

    Returns:
        (content, done_event, reasoning_parts, tool_calls_order)
    """
    accumulated_content: list[str] = []
    done_event: dict | None = None
    reasoning_parts: list[str] = []
    tool_calls_by_id: dict = {}
    tool_calls_order: list = []

    tool_id_mapping = (bridge_configurations.get(assigned_agent) or {}).get(
        "tool_id_and_name_mapping", {}
    )

    async for chunk in body_iterator:
        if isinstance(chunk, bytes):
            chunk = chunk.decode("utf-8")
        for line in chunk.split("\n"):
            line = line.strip()
            if not line.startswith("data: "):
                continue
            try:
                event = json.loads(line[6:])
            except json.JSONDecodeError:
                continue

            evt_type = event.get("event")

            if evt_type == "delta":
                piece = event.get("content", "")
                accumulated_content.append(piece)
                if streamer and forward_deltas:
                    await streamer.emit_task_delta(task_id, piece)

            elif evt_type == "reasoning":
                piece = event.get("content", "")
                if aggregate_metrics is not None and piece:
                    reasoning_parts.append(piece)
                if streamer and forward_reasoning:
                    await streamer.emit_task_reasoning(task_id, piece)

            elif evt_type == "tool_call":
                call_id = event.get("call_id", "") or f"tool_{task_id}_{len(tool_calls_order)}"
                if aggregate_metrics is not None and call_id not in tool_calls_by_id:
                    tool_name = event.get("name", "")
                    enriched_args = inject_variables_into_tool_args(
                        tool_name, event.get("args", {}),
                        variables, variables_path, tool_id_mapping,
                    )
                    entry = {call_id: {"name": tool_name, "args": enriched_args, "data": None, "id": tool_name}}
                    tool_calls_by_id[call_id] = entry[call_id]
                    tool_calls_order.append(entry)
                if streamer:
                    await streamer.emit_task_tool_call(
                        task_id,
                        name=event.get("name", ""),
                        args=event.get("args", {}),
                        call_id=call_id,
                    )

            elif evt_type == "tool_result":
                call_id = event.get("call_id", "") or f"tool_{task_id}_{len(tool_calls_order)}"
                result_content = event.get("content", "")
                if aggregate_metrics is not None:
                    result_data = {
                        "response": result_content,
                        "status": 1,
                        "metadata": {"type": "function"},
                    }
                    if call_id in tool_calls_by_id:
                        tool_calls_by_id[call_id]["data"] = result_data
                    else:
                        tool_calls_order.append({
                            call_id: {
                                "name": event.get("name", ""),
                                "args": {},
                                "data": result_data,
                                "id": event.get("name", ""),
                            }
                        })
                if streamer:
                    await streamer.emit_task_tool_result(
                        task_id,
                        name=event.get("name", ""),
                        content=result_content,
                        call_id=call_id,
                    )

            elif evt_type == "done":
                done_event = event

    content = "".join(accumulated_content)
    if done_event and (done_event.get("response") or {}).get("data", {}).get("content"):
        content = done_event["response"]["data"]["content"]

    return content, done_event, reasoning_parts, tool_calls_order
