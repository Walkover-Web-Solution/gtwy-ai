import json
import uuid

from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage
from langchain_core.tools import StructuredTool

from globals import logger
from workflow.llm import create_llm, extract_text_from_response, extract_json_from_text
from workflow.tool_adapter import normalize_tool_payload
from workflow.prompts import (
    PLANNER_SYSTEM_PROMPT,
    RESEARCH_SYSTEM_PROMPT,
    REPLAN_SYSTEM_PROMPT,
    WORKER_CLARIFICATION_PROMPT,
    Markers,
)


def _format_tool_schemas(tool_schemas: list[dict]) -> str:
    if not tool_schemas:
        return ""
    lines = ["## Available tools",
             "The executor has access to these tools. Plan tasks that use them.", ""]
    for tool in tool_schemas:
        params = tool.get("parameters", [])
        if params:
            param_parts = []
            for p in params:
                req = "required" if p.get("required") else "optional"
                param_parts.append(f"    - `{p['name']}` ({p.get('type','string')}, {req}): {p.get('description','')}")
            lines.append(f"**{tool['name']}** — {tool['description']}\n  Parameters:\n" + "\n".join(param_parts) + "\n")
        else:
            lines.append(f"**{tool['name']}** — {tool['description']}\n")
    return "\n".join(lines)


def _build_user_message(state: dict) -> str:
    parts = [f"GOAL: {state['goal']}"]
    if state.get("human_input"):
        parts.append(f"\nUser's answer to previous question: {state['human_input']}")
    scratchpad = state.get("scratchpad") or []
    if scratchpad:
        notes = "\n".join(f"- {item.get('note', '')}" for item in scratchpad)
        parts.append(f"\nAccumulated context from previous work:\n{notes}")
    return "\n".join(parts)


def _parse_tasks(parsed: dict) -> list[dict]:
    raw_tasks = parsed.get("tasks") or []
    task_ids = [str(uuid.uuid4())[:8] for _ in raw_tasks]
    tasks = []
    for index, raw_task in enumerate(raw_tasks):
        deps = []
        for dep in raw_task.get("depends_on") or []:
            if isinstance(dep, int) and 0 <= dep < len(task_ids):
                deps.append(task_ids[dep])
            elif isinstance(dep, str) and dep in task_ids:
                deps.append(dep)
        tasks.append({
            "id": task_ids[index],
            "title": raw_task.get("title", f"Task {index + 1}"),
            "description": raw_task.get("description", ""),
            "tool_name": raw_task.get("tool_name"),
            "status": "pending",
            "result": None,
            "depends_on": deps,
            "priority": raw_task.get("priority", "medium"),
            "acceptance_criteria": raw_task.get("acceptance_criteria", "Task completed successfully"),
            "estimated_complexity": raw_task.get("estimated_complexity", "moderate"),
            "reflection": None,
        })
    return tasks


def _build_replan_prompt(state: dict) -> str:
    completed = state.get("completed_tasks", [])
    completed_summary = "\n".join(
        f"- {c['title']}: {c['result'][:200]}" for c in completed
    ) if completed else "None"
    failed_task = "Unknown"
    failure_reason = state.get("replan_reason", "Unknown failure")
    for t in state.get("tasks", []):
        if t.get("status") == "failed":
            failed_task = f"{t['title']}: {t['description']}"
            if t.get("result"):
                failure_reason = t["result"]
            break
    scratchpad = state.get("scratchpad", [])
    scratchpad_text = "\n".join(
        f"- {s.get('note', '')}" for s in scratchpad
    ) if scratchpad else "Empty"
    return REPLAN_SYSTEM_PROMPT.format(
        goal=state["goal"],
        completed_summary=completed_summary,
        failed_task=failed_task,
        failure_reason=failure_reason,
        scratchpad=scratchpad_text,
    )


async def _run_research_phase(
    state: dict,
    tools: list[StructuredTool],
    model: str,
    temperature: float,
    base_prompt: str,
    user_message: str,
    max_rounds: int = 5,
) -> tuple[str, list]:
    """Tool-calling research loop before plan generation. Returns (research_context, thinking_steps)."""
    tools_by_name = {t.name: t for t in tools}
    research_llm = create_llm(
        model=model, api_key=state["api_key"], temperature=temperature,
        streaming=True, tools=tools,
        service=state.get("user_config", {}).get("planner_service", "openai"),
    )
    messages = [
        SystemMessage(content=RESEARCH_SYSTEM_PROMPT + "\n\n" + base_prompt),
        HumanMessage(content=user_message),
    ]
    research_notes: list[str] = []
    thinking_steps: list[dict] = []

    for round_num in range(max_rounds):
        response = await research_llm.ainvoke(messages)
        messages.append(response)
        if response.content:
            thinking_steps.append({"type": "reasoning", "content": response.content, "round": round_num + 1})
            if Markers.RESEARCH_DONE in response.content:
                summary = response.content.split(Markers.RESEARCH_DONE, 1)[-1].strip()
                if summary:
                    research_notes.append(summary)
                break
        if not response.tool_calls:
            break
        for tool_call in response.tool_calls:
            tool_fn = tools_by_name.get(tool_call["name"])
            if tool_fn is None:
                result = f"Unknown tool: {tool_call['name']}"
            else:
                try:
                    call_args = normalize_tool_payload(tool_fn, tool_call.get("args") or {})
                    result = await tool_fn.ainvoke(call_args)
                except Exception as e:
                    result = f"Tool error: {e}"
            result_str = result if isinstance(result, str) else json.dumps(result)
            research_notes.append(f"[{tool_call['name']}] {result_str[:400]}")
            thinking_steps.append({"type": "tool_call", "tool_name": tool_call["name"], "result": result_str[:400]})
            messages.append(ToolMessage(content=result_str, tool_call_id=tool_call["id"]))

    return "\n".join(research_notes), thinking_steps


def make_planner_node(tools: list[StructuredTool], tool_schemas: list[dict]):
    async def planner_node(state: dict) -> dict:
        config = state.get("user_config") or {}
        model = config.get("planner_model", "gpt-4o")
        temperature = config.get("planner_temperature", 0.3)
        is_replan = state.get("needs_replan", False)
        logger.info(f"[Workflow] Planner node started (replan={is_replan}, model={model})")

        # Worker clarification path — planner tries to resolve itself first, only escalates to user if it can't
        if state.get("needs_worker_clarification") and state.get("worker_question"):
            # Find the task the worker is stuck on
            stuck_task = {}
            for t in state.get("tasks", []):
                if t.get("id") == state.get("worker_question_task_id"):
                    stuck_task = t
                    break

            completed = state.get("completed_tasks") or []
            completed_summary = "\n".join(
                f"- {c['title']}: {c['result'][:200]}" for c in completed
            ) if completed else "None"
            plan_summary = "\n".join(
                f"- [{t['status']}] {t['title']}: {t['description'][:150]}" for t in state.get("tasks", [])
            ) if state.get("tasks") else "No tasks"

            clarification_prompt = WORKER_CLARIFICATION_PROMPT.format(
                goal=state["goal"],
                task_title=stuck_task.get("title", "Unknown"),
                task_description=stuck_task.get("description", ""),
                worker_question=state["worker_question"],
                completed_summary=completed_summary,
                plan_summary=plan_summary,
            )

            llm = create_llm(
                model=model, api_key=state["api_key"], temperature=temperature,
                json_mode=True, service=config.get("planner_service", "openai"),
            )
            try:
                response = await llm.ainvoke([
                    SystemMessage(content=clarification_prompt),
                    HumanMessage(content="Resolve the worker's question now."),
                ])
                response_text = extract_text_from_response(response)
                parsed_response = extract_json_from_text(response_text)
            except Exception as e:
                logger.error(f"[Workflow] Planner clarification LLM failed: {e}")
                parsed_response = {"mode": "escalate", "reasoning": str(e), "question": {"text": state["worker_question"], "options": []}}

            thinking = [{"type": "reasoning", "content": parsed_response.get("reasoning", "")}]

            if parsed_response.get("mode") == "answer":
                # Planner resolved it — send answer back to executor
                logger.info(f"[Workflow] Planner resolved worker question for task '{stuck_task.get('title')}'")
                return {
                    "planner_response": parsed_response["answer"],
                    "needs_worker_clarification": False,
                    "worker_question": None,
                    "planner_thinking": thinking,
                    "needs_question": False,
                    "worker_messages": state.get("worker_messages"),
                }
            else:
                # Planner can't resolve — escalate to user
                logger.info(f"[Workflow] Planner escalating worker question to user for task '{stuck_task.get('title')}'")
                escalate_q = parsed_response.get("question", {})
                return {
                    "needs_question": True,
                    "question_text": escalate_q.get("text", state["worker_question"]),
                    "question_options": escalate_q.get("options", []),
                    "planner_thinking": thinking,
                    "needs_worker_clarification": False,
                    "worker_messages": state.get("worker_messages"),
                }

        tool_block = _format_tool_schemas(tool_schemas)

        if is_replan:
            base_prompt = _build_replan_prompt(state)
            if tool_block:
                base_prompt = f"{base_prompt}\n\n{tool_block}"
        else:
            agent_persona = config.get("system_prompt", "")
            if agent_persona:
                base_prompt = (
                    f"You are acting as the planner for an AI agent with the following persona:\n"
                    f"---\n{agent_persona}\n---\n\n"
                    f"{PLANNER_SYSTEM_PROMPT}"
                )
            else:
                base_prompt = PLANNER_SYSTEM_PROMPT
            if tool_block:
                base_prompt = f"{base_prompt}\n\n{tool_block}"

        user_message = _build_user_message(state)

        # Research phase — only on fresh plans, not replans (replan already has full context)
        research_context = ""
        thinking_steps: list[dict] = []
        if tools and not is_replan:
            research_context, thinking_steps = await _run_research_phase(
                state, tools, model, temperature, base_prompt, user_message
            )

        plan_prompt = base_prompt
        if research_context:
            plan_prompt = (
                f"{base_prompt}\n\n"
                f"## Research findings (gathered via tool calls)\n"
                f"{research_context}"
            )

        llm = create_llm(
            model=model,
            api_key=state["api_key"],
            temperature=temperature,
            streaming=True,
            json_mode=True,
            service=config.get("planner_service", "openai"),
        )
        response = await llm.ainvoke([
            SystemMessage(content=plan_prompt),
            HumanMessage(content=user_message),
        ])
        # Safely extract text from response (handles Anthropic content blocks)
        response_text = extract_text_from_response(response)
        parsed = extract_json_from_text(response_text)
        reasoning = parsed.get("reasoning", "")
        if reasoning:
            thinking_steps.append({"type": "plan_reasoning", "content": reasoning})

        if parsed.get("mode") == "question" and parsed.get("question"):
            result: dict = {
                "needs_question": True,
                "question_text": parsed["question"].get("text", ""),
                "question_options": parsed["question"].get("options", []),
                "planner_thinking": thinking_steps,
            }
            if is_replan:
                result["needs_replan"] = True
                result["replan_reason"] = state.get("replan_reason")
            else:
                result["tasks"] = []
                result["needs_replan"] = False
                result["replan_reason"] = None
            return result

        new_tasks = _parse_tasks(parsed)
        response_schema = parsed.get("response_schema") or state.get("response_schema")
        revision_count = state.get("plan_revision_count", 0)

        if is_replan:
            revision_count += 1
            existing_tasks = state.get("tasks", [])
            preserved = [t for t in existing_tasks if t["status"] in ("completed", "skipped")]
            merged_tasks = preserved + new_tasks
            return {
                "needs_question": False,
                "question_text": None,
                "question_options": None,
                "tasks": merged_tasks,
                "human_input": None,
                "planner_thinking": thinking_steps,
                "plan_approved": not config.get("require_plan_approval", False),
                "step_approved": False,
                "needs_replan": False,
                "replan_reason": None,
                "plan_revision_count": revision_count,
                "needs_worker_clarification": False,
                "worker_question": None,
                "planner_response": None,
                "current_task_index": len(preserved),
                "response_schema": response_schema,
            }

        return {
            "needs_question": False,
            "question_text": None,
            "question_options": None,
            "tasks": new_tasks,
            "human_input": None,
            "planner_thinking": thinking_steps,
            "plan_approved": not config.get("require_plan_approval", False),
            "step_approved": False,
            "needs_replan": False,
            "replan_reason": None,
            "plan_revision_count": revision_count,
            "needs_worker_clarification": False,
            "worker_question": None,
            "planner_response": None,
            "current_task_index": 0,
            "response_schema": response_schema,
        }

    return planner_node
