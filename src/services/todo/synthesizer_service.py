import copy
import json
import uuid

from globals import logger
from src.services.todo.executor_service import _get_tasks, get_synthesizer_prompt


async def synthesize_results(
    bridge_id, bridge_configurations, parsed_data, final_plan, streamer=None,
):
    from src.services.commonServices.common import chat_multiple_agents

    tasks = _get_tasks(final_plan)
    if not tasks:
        return ""

    task_summary = "\n".join(
        f"- ✓ {tasks[t].get('title', t)} — completed" if tasks[t].get("status") == "completed"
        else f"- ✗ {tasks[t].get('title', t)} — {tasks[t].get('status', 'unknown')}"
        for t in tasks
    )
    goal = final_plan.get("goal") or parsed_data.get("user", "")

    prompt_template = await get_synthesizer_prompt()
    if not prompt_template:
        return ""

    try:
        system_prompt = prompt_template.format(goal=goal, task_summary=task_summary)
    except (KeyError, ValueError) as e:
        logger.error(f"[Synthesizer] Prompt template error: {e}")
        return ""

    if streamer:
        await streamer.emit_delta(json.dumps({"event": "synthesizer_start"}))

    synth_body = dict(parsed_data)
    synth_body["user"] = system_prompt
    synth_body["message_id"] = str(uuid.uuid1())
    synth_body["skip_history"] = True
    synth_body.pop("action", None)
    synth_body.pop("mode", None)
    synth_body.pop("task_id", None)

    # Only update prompt and clear tools — keep everything else (model, service) as-is
    scoped_bridge_configurations = dict(bridge_configurations)
    main_entry = copy.deepcopy(bridge_configurations.get(bridge_id) or {})
    main_config = main_entry.setdefault("configuration", {})
    main_config["tools"] = []
    main_config.pop("response_type", None)
    main_config["stream"] = True
    scoped_bridge_configurations[bridge_id] = main_entry
    synth_body["bridge_configurations"] = scoped_bridge_configurations

    chunks = []
    full_text = ""
    try:
        response = await chat_multiple_agents({"body": synth_body, "state": {}})

        if hasattr(response, "body_iterator"):
            async for chunk in response.body_iterator:
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
                        if piece:
                            chunks.append(piece)
                            if streamer:
                                await streamer.emit_delta(json.dumps({"event": "synthesizer_chunk", "content": piece}))
                    elif evt_type == "done":
                        done_content = (event.get("response") or {}).get("data", {}).get("content")
                        if done_content:
                            full_text = done_content

        elif hasattr(response, "body"):
            payload = json.loads(response.body.decode("utf-8"))
            if payload.get("success"):
                full_text = payload.get("response", {}).get("data", {}).get("content", "")

    except Exception as e:
        logger.error(f"[Synthesizer] Error: {e}")
        return ""

    if not full_text:
        full_text = "".join(chunks)

    if streamer and full_text:
        await streamer.emit_delta(json.dumps({"event": "synthesizer_done", "content": full_text}))

    return full_text
