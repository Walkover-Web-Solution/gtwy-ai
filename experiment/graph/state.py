from typing import Optional, TypedDict


class TaskItem(TypedDict):
    id: str
    title: str
    description: str
    tool_name: Optional[str]  # which tool to call for this task (assigned by planner)
    status: str  # "pending" | "in_progress" | "completed" | "failed" | "skipped"
    result: Optional[str]
    depends_on: list[str]  # IDs of tasks this depends on
    priority: str  # "high" | "medium" | "low"
    acceptance_criteria: str  # what "done" looks like for this task
    estimated_complexity: str  # "simple" | "moderate" | "complex"
    reflection: Optional[str]  # self-reflection result after execution


class UserConfig(TypedDict, total=False):
    """User/agent configuration that flows through the graph.
    All fields are optional — nodes fall back to sensible defaults."""
    planner_model: str           # model for planner node (default: gpt-4o)
    planner_temperature: float   # planner temperature (default: 0.3)
    executor_model: str          # model for executor/worker node (default: gpt-4o-mini)
    executor_temperature: float  # executor temperature (default: 0.5)
    synthesizer_model: str       # model for synthesizer node (default: gpt-4o-mini)
    direct_model: str            # model for direct mode node (default: gpt-4o-mini)
    max_tokens: int              # max output tokens (default: 4096)
    system_prompt: str           # custom persona / system prompt
    enable_reflection: bool      # whether executor self-reflects (default: True)
    max_retries: int             # max executor retries on quality failure (default: 2)


class AgentState(TypedDict):
    thread_id: str
    goal: str
    mode: str  # "plan" or "direct" — user selected from UI
    api_key: str
    agent_id: Optional[str]  # ID of the agent being executed (None = default)
    user_config: UserConfig  # user/agent configuration for model selection, temperatures, etc.
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
    tool_schemas: list  # tool metadata injected into planner [{name, description, parameters}]
    planner_thinking: list  # planner research/reasoning steps [{type, content, tool_name?, result?}]
    plan_revision_count: int  # how many times the plan has been revised
    needs_replan: bool  # True when worker signals a re-plan is needed
    replan_reason: Optional[str]  # why re-planning was triggered
    needs_worker_clarification: bool  # True when worker asks planner a question
    worker_question: Optional[str]  # the question from worker to planner
    worker_question_task_id: Optional[str]  # which task the worker was executing when it asked
    planner_response: Optional[str]  # planner's answer back to the worker
