from typing import Optional, TypedDict


class TaskItem(TypedDict):
    id: str
    title: str
    description: str
    status: str  # "pending" | "in_progress" | "completed" | "failed" | "skipped"
    result: Optional[str]
    depends_on: list[str]  # IDs of tasks this depends on
    priority: str  # "high" | "medium" | "low"
    acceptance_criteria: str  # what "done" looks like for this task
    estimated_complexity: str  # "simple" | "moderate" | "complex"
    reflection: Optional[str]  # self-reflection result after execution


class AgentState(TypedDict):
    thread_id: str
    goal: str
    mode: str  # "plan" or "direct" — user selected from UI
    api_key: str
    agent_id: Optional[str]  # ID of the agent being executed (None = default)
    tasks: list[TaskItem]
    completed_tasks: list[dict]
    current_task_index: int
    final_answer: Optional[str]
    needs_question: bool  # True when AI wants to ask the user something
    question_text: Optional[str]
    question_options: Optional[list[str]]
    human_input: Optional[str]  # filled when user answers via WebSocket
    plan_approved: bool  # True when user has approved the full plan
    step_approved: bool  # True when user has approved the current step to execute
    step_feedback: Optional[str]  # Optional rejection/feedback message from user
    direct_messages: list  # Conversation history for direct mode (list of {role, content})
    built_steps: list  # Accumulated automation steps built so far (for FBAI flow)
    scratchpad: list  # shared memory across worker executions [{note, source_task_id}]
    plan_revision_count: int  # how many times the plan has been revised
    needs_replan: bool  # True when worker signals a re-plan is needed
    replan_reason: Optional[str]  # why re-planning was triggered
