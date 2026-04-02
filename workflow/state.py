from typing import Optional, TypedDict


class TaskItem(TypedDict):
    id: str
    title: str
    description: str
    tool_name: Optional[str]
    status: str
    result: Optional[str]
    depends_on: list[str]
    priority: str
    acceptance_criteria: str
    estimated_complexity: str
    reflection: Optional[str]
    worker_thread_id: Optional[str]


class UserConfig(TypedDict, total=False):
    planner_model: str
    planner_temperature: float
    executor_model: str
    executor_temperature: float
    synthesizer_model: str
    require_plan_approval: bool
    require_step_approval: bool
    max_tokens: int
    system_prompt: str
    enable_reflection: bool
    max_retries: int


class WorkflowState(TypedDict):
    # --- Core ---
    run_id: Optional[str]
    thread_id: str
    goal: str
    api_key: str
    user_config: UserConfig
    runtime_variables: Optional[dict]

    # --- Planning ---
    tasks: list[TaskItem]
    completed_tasks: list[dict]
    current_task_index: int
    final_answer: Optional[str]
    scratchpad: list
    tool_schemas: list
    planner_thinking: list
    plan_revision_count: int
    needs_replan: bool
    replan_reason: Optional[str]
    response_format: Optional[dict]
    response_schema: Optional[dict]

    # --- Human interaction ---
    needs_question: bool
    question_text: Optional[str]
    question_options: Optional[list[str]]
    human_input: Optional[str]
    plan_approved: bool
    step_approved: bool
    step_feedback: Optional[str]

    # --- Worker clarification ---
    needs_worker_clarification: bool
    worker_question: Optional[str]
    worker_question_task_id: Optional[str]
    planner_response: Optional[str]
    worker_messages: Optional[list]
