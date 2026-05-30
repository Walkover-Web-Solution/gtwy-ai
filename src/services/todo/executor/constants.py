# Statuses a task can no longer transition out of without an explicit user
# action. `waiting_for_user` is intentionally NOT terminal — it's a pause
# point: the orchestrator must stop the executor loop and emit `plan_paused`,
# then resume from the same task once the user provides answers via the
# `respond` action.
TERMINAL_STATUSES = {"completed", "failed", "skipped"}

INTERRUPTED_TASK_RECOVERY_NOTE = (
    "Task was in_progress when execution was interrupted; reset to pending for retry"
)

WORKER_RESPONSE_SCHEMA = """
{
  "status": "completed | waiting_for_user | failed",
  "result": "final answer / tool output when status=completed, else null",
  "questions": [
    { "id": "q1", "question": "what you need from the user", "options": ["pick-list options or [] for free-form"] }
  ],
  "error": "one-line cause when status=failed, else null",
  "history": {
    "goal": "one-line task goal — set once, never change",
    "previous_questions": [
      { "id": "q1", "question": "what you asked", "answer": "user's reply or null if still unanswered" }
    ],
    "attempts": 1,
    "next_step": "one-line plan for the NEXT turn — e.g. 'call send_email tool with q2 answer' or 'wait for q3'",
    "notes": "short running log: what you tried, what you learned, what's blocking"
  }
}"""

WORKER_SYSTEM_PROMPT_TEMPLATE = """You speak DIRECTLY to the end user — there is no parent agent translating. Return strict JSON matching the schema below.

## Task
{title}: {task_description}

## Execution Details (params from planner — use as-is)
{execution_details}

{human_response_block}## Tool
{tool_list}

## State machine — pick exactly one status
- All required info present → call the tool, set `status: completed`, put output in `result`.
- Missing required info → set `status: waiting_for_user`, list each missing field in `questions` (unique id, clear question, options or []).
- USER_CLARIFICATION_ANSWER present → match each answer to its question id, write it into `history.previous_questions[].answer`, then re-decide (completed or ask remaining).
- Unrecoverable after 2 retries → set `status: failed`, one-line cause in `error`.

## history (REQUIRED every turn — this is your scratchpad across turns)
- goal: state once; copy verbatim on every later turn.
- previous_questions: APPEND each new question you ask; on a continuation, fill the matching id's `answer`. NEVER drop earlier entries.
- attempts: 1 on first try; +1 on each retry of the same step.
- next_step: one line — what you will do on the NEXT turn (concrete: tool name, which question's answer you need, "finish" if done).
- notes: append-only log. Brief.

## Response (JSON only, no markdown, no prose outside the JSON)
{schema}"""
