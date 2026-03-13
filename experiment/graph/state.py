from typing import Optional, TypedDict


class TaskItem(TypedDict):
    id: str
    title: str
    description: str
    status: str  # "pending" | "in_progress" | "completed" | "failed"
    result: Optional[str]


class AgentState(TypedDict):
    thread_id: str
    goal: str
    mode: str  # "plan" or "direct" — user selected from UI
    api_key: str
    tasks: list[TaskItem]
    completed_tasks: list[dict]
    current_task_index: int
    final_answer: Optional[str]
    needs_question: bool  # True when AI wants to ask the user something
    question_text: Optional[str]
    question_options: Optional[list[str]]
    human_input: Optional[str]  # filled when user answers via WebSocket
    plan_approved: bool  # True when user has approved the plan
