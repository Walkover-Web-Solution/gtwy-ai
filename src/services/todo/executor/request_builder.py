import copy
import uuid

from src.services.todo.executor.plan_tasks import get_tasks
from src.services.todo.executor.prompt_builder import build_worker_system_prompt, build_worker_user_message
from src.services.todo.executor.prompt_template_loader import get_worker_prompt_template
from src.services.todo.executor.tools import filter_tools_for_task


def _base_request(assigned_agent, thread_id, sub_thread_id, org_id,
                  bridge_configurations, plan, variables, variables_path, skip_history):
    """Shared request body fields used by both worker and connected-agent paths."""
    return {
        "bridge_id": assigned_agent,
        "message_id": str(uuid.uuid1()),
        "thread_id": thread_id,
        "sub_thread_id": sub_thread_id,
        "org_id": org_id,
        "variables": variables or {},
        "variables_path": variables_path or {},
        "bridge_configurations": bridge_configurations,
        "plans": plan,
        "skip_history": skip_history,
    }


async def build_worker_request(
    task_id: str,
    task: dict,
    bridge_id: str,
    thread_id: str,
    sub_thread_id: str,
    org_id: str,
    bridge_configurations: dict,
    plan: dict,
    variables: dict,
    variables_path: dict,
    streamer=None,
) -> tuple[dict, dict]:
    """Build scoped config + request body for a primary-agent (worker) task.

    Overrides the agent's prompt, tool list, and response type so the worker
    follows the executor JSON contract.

    Returns: (request_body, current_agent_config)
    """
    current_agent_config = bridge_configurations.get(bridge_id, {})

    # Deep-copy the main bridge entry so per-task changes don't leak to siblings.
    scoped_bridge_configurations = dict(bridge_configurations)
    scoped_agent_entry = copy.deepcopy(scoped_bridge_configurations.get(bridge_id) or {})
    scoped_bridge_configurations[bridge_id] = scoped_agent_entry
    scoped_agent_config = scoped_agent_entry.setdefault("configuration", {})

    filtered_tools = filter_tools_for_task(scoped_agent_entry, task.get("assigned_tool"))
    filtered_tool_names = [
        t.get("name") or (t.get("function") or {}).get("name") or ""
        for t in filtered_tools
    ]
    filtered_tool_names = [n for n in filtered_tool_names if n]
    scoped_agent_config["tools"] = filtered_tools

    worker_prompt_template = await get_worker_prompt_template()
    scoped_agent_config["prompt"] = build_worker_system_prompt(
        task, filtered_tool_names, get_tasks(plan), prompt_template=worker_prompt_template,
    )
    scoped_agent_config["response_type"] = {"type": "json_object"}

    fall_back = (current_agent_config.get("settings") or {}).get("fall_back") or {}
    if fall_back.get("is_enable") and fall_back.get("model"):
        scoped_agent_config["model"] = fall_back["model"]
        if fall_back.get("service"):
            scoped_agent_entry["service"] = fall_back["service"]

    if streamer:
        scoped_agent_config["stream"] = True

    request_body = {
        **_base_request(bridge_id, thread_id, sub_thread_id, org_id,
                        scoped_bridge_configurations, plan, variables, variables_path,
                        skip_history=True),
        "user": build_worker_user_message(task_id, task),
    }

    return request_body, current_agent_config


