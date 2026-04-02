from langgraph.types import interrupt


def _normalize_text_response(value) -> str:
    if isinstance(value, dict):
        return str(
            value.get("answer")
            or value.get("response")
            or value.get("text")
            or value.get("feedback")
            or ""
        ).strip()
    return str(value or "").strip()


def _normalize_approval_response(value) -> tuple[bool, str]:
    if isinstance(value, bool):
        return value, ""
    if isinstance(value, dict):
        approved_value = value.get("approved")
        if approved_value is None:
            action = str(value.get("action", "")).strip().lower()
            approved_value = action in {"approve", "approved", "yes", "true", "continue"}
        feedback = _normalize_text_response(value)
        return bool(approved_value), feedback
    text = str(value or "").strip()
    lowered = text.lower()
    if lowered in {"approve", "approved", "yes", "y", "true", "continue"}:
        return True, ""
    if lowered in {"reject", "rejected", "no", "n", "false"}:
        return False, ""
    return bool(text), text


def _build_plan_approval_payload(state: dict) -> dict:
    return {
        "type": "plan_approval",
        "run_id": state.get("run_id"),
        "goal": state.get("goal"),
        "planner_thinking": state.get("planner_thinking") or [],
        "tasks": state.get("tasks") or [],
    }


def _build_human_question_payload(state: dict) -> dict:
    payload_type = "worker_clarification" if state.get("worker_question_task_id") else "planner_question"
    return {
        "type": payload_type,
        "run_id": state.get("run_id"),
        "goal": state.get("goal"),
        "question": state.get("question_text") or state.get("worker_question") or "",
        "options": state.get("question_options") or [],
        "planner_thinking": state.get("planner_thinking") or [],
        "task_id": state.get("worker_question_task_id"),
    }


def _build_step_approval_payload(state: dict) -> dict:
    tasks = state.get("tasks") or []
    current_index = state.get("current_task_index", 0)
    next_task = tasks[current_index] if 0 <= current_index < len(tasks) else None
    return {
        "type": "step_approval",
        "run_id": state.get("run_id"),
        "goal": state.get("goal"),
        "task": next_task,
        "completed_tasks": state.get("completed_tasks") or [],
    }


def make_human_input_node():
    async def human_input_node(state: dict) -> dict:
        answer = interrupt(_build_human_question_payload(state))
        normalized_answer = _normalize_text_response(answer)
        update = {
            "human_input": normalized_answer,
            "needs_question": False,
            "question_text": None,
            "question_options": [],
            "needs_worker_clarification": False,
            "worker_question": None,
        }
        if state.get("worker_question_task_id"):
            update["planner_response"] = normalized_answer
        return update

    return human_input_node


def make_plan_approval_node():
    async def plan_approval_node(state: dict) -> dict:
        approved, feedback = _normalize_approval_response(interrupt(_build_plan_approval_payload(state)))
        if approved:
            return {
                "plan_approved": True,
                "step_approved": False,
                "step_feedback": None,
                "human_input": None,
            }
        return {
            "plan_approved": False,
            "step_approved": False,
            "step_feedback": feedback or "Plan approval was rejected.",
            "human_input": feedback or "The proposed plan was rejected. Revise it and try again.",
            "planner_response": None,
            "tasks": [],
        }

    return plan_approval_node


def make_step_approval_node():
    async def step_approval_node(state: dict) -> dict:
        approved, feedback = _normalize_approval_response(interrupt(_build_step_approval_payload(state)))
        if approved:
            return {
                "step_approved": True,
                "step_feedback": None,
                "human_input": None,
            }
        return {
            "step_approved": False,
            "step_feedback": feedback or "Step approval was rejected.",
            "needs_replan": True,
            "replan_reason": feedback or "The next step was rejected and needs a revised plan.",
            "human_input": feedback or "The next step was rejected. Revise the plan.",
        }

    return step_approval_node
