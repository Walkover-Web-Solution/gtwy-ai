---
covers: ["src/services/todo/**"]
verified_at: 6fdb8181
verified_on: 2026-10-13
---
# Todo mode (plan mode)

Plan mode splits one user goal into a list of tasks, runs them (in parallel where dependencies allow) on the main agent or on connected agents, and writes one final answer at the end. The code lives in `src/services/todo/`. It is reached only through `src/services/commonServices/common.py:chat` when the request body has `"mode": "plan"`. There are three roles: a **planner** (one normal chat call that returns plan JSON), an **executor** (one "worker" chat call per task), and a **synthesizer** (one chat call that writes the final reply).

| File | Role |
|---|---|
| `src/services/todo/__init__.py` | Re-exports `handle_todo_mode`. `common.py` imports from the submodules directly. |
| `src/services/todo/planner_service.py` | `prepare_planner_request`: rewrites the planner call in place (tools, prompt, conversation, JSON output). `_parse_plan_json` is shared with the plan saver and the replanner. |
| `src/services/todo/plan_store.py` | Redis CRUD for the plan and the planner Q&A session. `PLAN_TTL` = 48 h. |
| `src/services/todo/todo_handler.py` | `handle_todo_mode`: the dispatcher for `action` requests (`approve`, `respond`, `retry`, `status`, `cancel`). Always returns SSE. |
| `src/services/todo/executor_service.py` | `execute_plan` (the scheduling loop), `_execute_single_task` (one worker call), `resume_task`, worker prompt template, metrics aggregation, the unreachable `_trigger_replan`. |
| `src/services/todo/synthesizer_service.py` | `synthesize_results`: one streamed chat call that turns the task outcomes into the final answer. |

Code outside the folder that belongs to this mode: `src/services/utils/common_utils.py:_save_plan_from_result` (saves the planner output), the plan-mode branch in `src/services/commonServices/common.py:chat`, the `task_*`, `planning` and `execution` emitters in `src/services/commonServices/streaming_service.py:StreamingService`, and `src/db_services/metrics_service.py:publish_plan_history_update`.

## How it is switched on

Plan mode is a **per-request body field**, not an agent setting. Nothing in the agent config or Mongo turns it on.

| Body field | Meaning | Read in |
|---|---|---|
| `mode` | `"plan"` turns the mode on. Any other value is ignored. | `src/services/utils/common_utils.py:parse_request_body` → `parsed_data["mode"]` |
| `action` | Missing: a **planner call** (make or update the plan). Set: one of `approve`, `respond`, `retry`, `status`, `cancel`. | same |
| `task_id` | Required for `respond` and `retry`. | same |
| `thread_id`, `sub_thread_id` | Scope of the plan. `sub_thread_id` defaults to `thread_id`. | same |
| `user` | Planner call: the goal or the user's change request. `respond`: the answer to the task's question. | same |

These fields are not declared on `CompletionRequest`. They pass because it has `extra="allow"`. The usual entry is `POST /api/v2/model/chat/completion` (see [api](../api.md)). The check sits after Step 10 of `chat` (see [chat-orchestration](chat-orchestration.md)):

```python
if parsed_data.get("mode") == "plan":
    if parsed_data.get("action"):
        return await handle_todo_mode(parsed_data=..., bridge_configurations=...)   # executor path
    await prepare_planner_request(parsed_data, bridge_configurations, custom_config)  # planner path, falls through to the normal LLM call
```

So by the time either branch runs, `chat` has already run pre-tools, thread loading (`manage_threads`), prompt and variable replacement, and custom-config building, for action requests too.

## How it works

### 1. Planner call (`mode=plan`, no `action`)

1. `prepare_planner_request` loads the existing plan and the planner session from Redis.
2. It splits the agent's `configuration.tools` with `_separate_search_and_other_tools`. A tool counts as a **search tool** when its `properties` has a `search` key. The planner LLM gets only the search tools. A tool that also has an `executor` property is in both lists. All other tools, and every connected agent in `bridge_configurations` (name, `bridge_id`, tool names), are written into the system prompt as "available for task execution".
3. It replaces `configuration.conversation` with a single assistant message holding the old plan's `history_summary`, or with `[]`. The thread history that `manage_threads` loaded is dropped.
4. It builds the system prompt with `_build_planner_system_prompt`: **the agent's own prompt** + `Available Agents and Tools:` + (on update) `Previously built plan by you (AI):` with the old `plan["plan"]` JSON.
5. It sets `custom_config["response_type"] = {"type": "json_object"}`.
6. The normal LLM call runs. When streaming, `chat` emits `start` then `planning` before the deltas.
7. `process_background_tasks` calls `_save_plan_from_result`. It parses the JSON (markdown fences are stripped), stamps `org_id`, `bridge_id`, `thread_id`, `sub_thread_id` and `message_id`, and calls `plan_store.save_plan`. It also puts the plan into `historyParams["plans"]`, so the history row for this message carries it. In the SSE path this runs before `done` is sent, so the plan is in Redis when the client sees `done`.

The platform planner prompt is empty. `PLANNER_PROMPT` is `""`, `_get_planner_prompt_from_db` always returns that default even when Mongo `preBuiltPrompts.planner_prompt` exists, and `_build_planner_system_prompt` ignores its `prompt` argument anyway. **The agent's own system prompt must tell the model to return plan JSON in the shape below.**

### 2. Execute (`action=approve`, `retry`, `respond`)

`handle_todo_mode` loads the plan, creates a `StreamingService(mode="sse")`, starts `_stream_plan_action` as a background task, and returns a `StreamingResponse` right away.

- `approve`: resets every `failed` task to `pending` (`retry=0`, `error=None`), sets `state="approved"`, then runs the executor.
- `retry`: resets one task (`task_id`) to `pending` and clears `result`, `error` and `is_error`, then runs the executor.
- `respond`: `resume_task` checks that the task is `waiting_for_user`, stores `user` as `human_response`, sets the task to `pending` and adds the Q&A to the planner session. The handler then replays every settled task as `task_started` + `task_completed`/`task_error` with `"replayed": true`, and runs the executor.

`execute_plan` loop (at most `max_iterations = 1000` passes):

1. Re-read the plan from Redis. At the start, `_recover_interrupted_in_progress_tasks` resets any `in_progress` task to `pending` (a crash left it there) and emits `tasks_recovered`.
2. `_is_plan_complete` (every task is in `TERMINAL_STATUSES`): set `state` to `failed` if any task failed, else `completed`. Emit `plan_completed`. Stop.
3. `_is_plan_blocked` (a task is `waiting_for_user`, or a `needs_replan` task with nothing running, or pending tasks of which none can start while nothing is running): `state="paused"`, emit `plan_paused`, stop.
4. `_get_runnable_tasks` returns `pending` tasks whose `dependencies` are all `completed`. If there are none, sleep 1 s and loop.
5. Mark them `in_progress` (+`started_at`), emit `task_started`, save, then run **all of them at once** with `asyncio.gather`. There is no concurrency cap.
6. Fold the results back in: `completed` → store `result`. `waiting_for_user` → store `human_query`. Failure → `retry += 1`, then back to `pending` while `retry < max_retry` (default 2), else `failed`. Emit `task_completed`, `task_waiting_for_user` or `task_error` (with `retrying`).

`_execute_single_task` (one worker):

1. The agent is `task.assigned_agent` or the main `bridge_id`. If that id is not in `bridge_configurations`, it falls back to the main agent.
2. It deep-copies that agent's entry and changes only the copy. `tools` = `_filter_tools_for_task` on `assigned_tool` (exact name match, then case-insensitive, else **no tools**). `prompt` = the worker template filled with the task (`title`, `task_description`, `execution_details`, the results of completed dependencies, `human_response`). `response_type` = `json_object`. `stream` = true.
3. If the agent has `settings.fall_back.is_enable` and a `model`, the worker **always runs on the fallback model** (it does not wait for an error). For wallet-billed agents, `src/services/billing/billing_utils.py:resolve_wallet_fallback_key` must accept the target model first. If it refuses, the original model is kept.
4. It calls `chat_multiple_agents` with a fresh `message_id`, the request `variables` and `variables_path`, `plans` (the plan snapshot), and `skip_history = True` for main-agent tasks. Connected-agent tasks save their own history rows.
5. It forwards the worker's stream as `task_delta`, `task_reasoning`, `task_tool_call` and `task_tool_result` events tagged with `task_id`. For main-agent tasks it adds up tokens, cost, latency, tool calls and reasoning into `main_agent_metrics`.
6. It parses the reply with `_parse_worker_response` and `_build_worker_result`.

### 3. Synthesize and finish

After `execute_plan` returns, the handler re-reads the plan and calls `synthesize_results` **whatever the end state is** (completed, paused or failed). The synthesizer fills Mongo `synthesizer_prompt` with `{goal}` and a ✓/✗ `{task_summary}`, then calls the main agent with tools cleared, `response_type` removed, streaming on and `skip_history=True`. It emits `synthesizer_start`, `synthesizer_chunk`… and `synthesizer_done`. If it returns `""` (no prompt, a template error, or a call error), `done.response.data.content` is the `result` of the highest-numbered completed task instead. If that result is a JSON string with a `data` key, `data` is used.

Then `emit_done` runs, and `publish_plan_history_update` is fired off (not awaited). It sends an `update_history` message to the log queue for the **planning** message's row (`plan["message_id"]`). The row gets the final plan in `plans`, the final text, and the summed main-agent metrics. See [queues-history-metrics](queues-history-metrics.md).

### 4. `status` and `cancel`

- `status`: `done` whose `response.data.content` is the whole plan as a JSON string.
- `cancel`: sets `state="failed"` and sends `done` with `response.data.finish_reason="cancelled"`. It does not stop a running executor.

## Data: the plan in Redis

| Key (after the `AIMIDDLEWARE_<ENVIRONMENT>_` prefix that `store_in_cache` adds) | Holds | TTL |
|---|---|---|
| `nd_plan_{org_id}_{bridge_id}_{thread_id}_{sub_thread_id}` | the plan | `PLAN_TTL` 172800 s, reset on every save |
| `nd_plan_session_{org_id}_{bridge_id}_{thread_id}_{sub_thread_id}` | planner Q&A session (`qa_history`, newest 25 kept) | same |

The prefix `nd_plan_` is `redis_keys["plan_"]` in `src/configs/constant.py`. Plans are never deleted on completion (`delete_plan` has no callers). They expire 48 h after the last write. The final plan is also kept in Postgres `conversation_logs.plans` (`models/postgres/pg_models.py:ConversationLog`) through the history update. See [data-model](../data-model.md).

Plan shape. Fields marked LLM come from the planner JSON exactly as returned. The code checks none of them except that the output is a JSON object.

```jsonc
{
  "plan": {                              // LLM. _get_tasks reads plan["plan"]["tasks"]
    "tasks": [                           // list (preferred) or dict keyed by id
      {
        "id": "task_1",                  // LLM. Must be "task_<int>" (see gotchas)
        "title": "…", "task_description": "…",
        "execution_details": "…",        // exact tool params for the worker (str or object)
        "assigned_agent": null,          // bridge_id of a connected agent, or null = main agent
        "assigned_tool": "tool_name",    // str or [str]; null = worker gets no tools
        "dependencies": ["task_0"],
        "status": "pending",             // pending | in_progress | completed | failed | waiting_for_user | skipped | needs_replan
        "max_retry": 2,                  // optional, default 2
        "retry": 0, "result": null, "error": null, "is_error": false,   // executor writes
        "human_query": null, "human_response": null,                     // executor / resume_task write
        "started_at": 0.0, "last_recovery_reason": "…"                   // executor writes
      }
    ],
    "goal": "…"                          // only written by _trigger_replan (unreachable)
  },
  "goal": "…",                           // LLM, optional. Read by the synthesizer and the planner message
  "questions": [],                       // LLM, optional. Planner clarification questions
  "history_summary": "…",               // LLM, optional. Becomes the planner's only conversation turn on the next update
  "state": "…",                          // LLM at creation; then approved | executing | paused | completed | failed
  "org_id": "…", "bridge_id": "…", "thread_id": "…", "sub_thread_id": "…",
  "message_id": "…",                     // planner call that saved this plan; history update target
  "created_at": "iso", "updated_at": "iso"   // plan_store
}
```

`skipped` only comes from the LLM. No code at this commit sets `needs_replan` (see "Why").

## Contracts

**Worker reply** (`WORKER_RESPONSE_SCHEMA`, enforced by `_parse_worker_response` / `_build_worker_result`):

```json
{"status": "completed | waiting_for_user | failed", "result": "...", "reasoning": "...", "human_query": "...", "error": "..."}
```

`needs_planner` and any unknown status become `waiting_for_user`. A reply that is not JSON, or has no `status`, becomes `waiting_for_user` with a generic "please provide guidance" question. Only `failed` (or a chat error or exception) goes down the retry path.

**SSE stream of an action request** (`data: {json}\n\n`):

| Event | How it arrives |
|---|---|
| `start`, `execution`, `done`, `error` | top-level events |
| `task_delta`, `task_reasoning`, `task_tool_call`, `task_tool_result` | top-level, with `task_id` |
| `execution_started`, `tasks_recovered`, `task_started`, `task_completed`, `task_waiting_for_user`, `task_error`, `plan_completed`, `plan_paused`, `plan_failed`, `plan_revised`, `plan_revision_failed`, `synthesizer_start`, `synthesizer_chunk`, `synthesizer_done` | **inside a `delta`**: `{"event":"delta","content":"{\"event\":\"task_started\",…}"}` |

`done.finish_reason` comes from the plan state: `completed` → `stop`, `paused` → `paused`, `failed` → `error`, anything else → `stop`. `done.usage` and `response.usage` are always zero. Real usage only reaches the history row.

**Prompts from Mongo `preBuiltPrompts`**: `worker_prompt` (placeholders `{title}`, `{task_description}`, `{execution_details}`, `{human_response_block}`, `{tool_list}`, `{schema}`) and `synthesizer_prompt` (`{goal}`, `{task_summary}`). Both are read in one call by `_get_worker_prompt_template_from_db` and cached in module globals. See [agent-configuration](agent-configuration.md) for `src/services/prebuilt_prompt_service.py`.

## Limits

| Limit | Value | Where |
|---|---|---|
| Executor loop passes | 1000, then `state="failed"` + `plan_failed` (`max_iterations_reached`) | `execute_plan` |
| Idle wait when nothing can run yet | 1 s per pass | `execute_plan` |
| Runs per task | `max_retry` (default 2) total attempts, counted by `retry` | `execute_plan` |
| Parallel workers | unbounded, all runnable tasks of a pass | `asyncio.gather` in `execute_plan` |
| Tool rounds inside one worker | the agent's normal limit | [tool-calling](tool-calling.md) |
| Plan / session lifetime | 48 h since last write | `PLAN_TTL` |
| Stored Q&A pairs | 25 | `_MAX_QA_HISTORY_STORED` |

## Failure behaviour

- **Planner returns bad JSON or empty content**: `_save_plan_from_result` logs and keeps the old plan. The client still gets a normal `done` with the raw text. There is no error event.
- **Redis write fails**: `store_in_cache` logs and returns `False`. The plan is silently not saved, and the next action gets `error: "No plan found for action: …"`.
- **Action with no plan**: `error` event, stream closes, no `done`.
- **`respond`/`retry` without `task_id`, unknown task, or a task not waiting**: `error` event.
- **Worker exception or `success: false`**: counted as a failure, retried up to `max_retry`, then `failed`. The plan ends `failed` once everything is terminal.
- **Unhandled exception in `_stream_plan_action`**: one `error` event with the message. `finally` always closes the stream.
- **Unknown `action` value with an existing plan**: only `start` is sent, then the stream closes.
- **Process restart mid-run**: tasks stay `in_progress` in Redis until the next `approve`/`retry`/`respond`, which resets them to `pending`.

## Gotchas

> [!WARNING]
> **The agent prompt is the planner prompt.** No platform planner prompt is added. If the agent's own prompt does not ask for `{"plan": {"tasks": [...]}}` JSON, nothing breaks loudly: the plan is saved with zero tasks, and `approve` then completes at once.

> [!WARNING]
> **Task ids must be `task_<int>`.** `_format_plan_response` sorts with `int(x.split("_")[1])` without a digit check. An id like "fetch_data" raises `ValueError` when the plan completed and the synthesizer returned nothing, and the client gets `error` instead of `done`. The `respond` replay guards with `isdigit()`. This one does not.

> [!WARNING]
> **`waiting_for_user` counts as terminal.** It is in `TERMINAL_STATUSES`, and `_is_plan_complete` runs before `_is_plan_blocked`. So if the only unfinished task is waiting and nothing depends on it, the plan ends `completed` (finish reason `stop`), not `paused`. The question still goes out as `task_waiting_for_user`, and `respond` still works.

> [!WARNING]
> **The metric helpers are defined twice.** `_init_main_agent_metrics`, `_merge_task_metrics` and `finalize_main_agent_metrics` each appear twice in `src/services/todo/executor_service.py`. Python uses the second copy. An edit to the first copy does nothing. `_get_tasks`/`_set_tasks` also exist in both `plan_store.py` and `executor_service.py` and must stay identical.

> [!WARNING]
> **No locking.** Every step is get → change → `save_plan` on one Redis key. Two action requests on the same thread (a double `approve`, or `respond` while a run is going) run two executors. The second one's recovery step resets the first one's `in_progress` tasks to `pending`, so tools run twice. `cancel` only changes `state`. A running loop keeps going and overwrites the state when it ends.

> [!WARNING]
> **Actions only work over direct SSE.** `handle_todo_mode` always uses an SSE `StreamingService`. If `chat_completion` queues the request (an RTLayer or webhook `response_format`, which includes playground calls with streaming off), the queue consumer drops the `StreamingResponse`. The plan still runs, but no event reaches anyone.

> [!WARNING]
> **The prompt caches are per process and never refreshed.** After the first worker call, `worker_prompt` and `synthesizer_prompt` stay as loaded until restart. If the fetch fails once, the flag is still set, so the process uses the default worker template and **skips the synthesizer** until restart. The synthesizer prompt is only loaded as a side effect of a worker call. A process that has not run a worker yet (for example an `approve` on a plan whose tasks are all already completed) skips synthesis.

> [!WARNING]
> **A missing `thread_id` does not give a 400.** `manage_threads` makes up a new uuid before the plan-mode branch runs. So the 400 in `handle_todo_mode` can't be reached from `chat`. An action without `thread_id` gets "No plan found". A planner call without `thread_id` saves under a fresh thread id, which the client must read from the response.

> [!WARNING]
> **Any existing plan means "update".** A new goal on a thread that already has a plan (even a completed one) goes to the planner with the old plan in its prompt. Use a new `sub_thread_id` to start fresh.

> [!WARNING]
> **The replanned plan replaces the old one.** `_save_plan_from_result` stores the planner output as-is. Completed tasks, results and `human_response` values survive only if the LLM copies them back from the old plan in its prompt. The docstring still describes a merge guard that #1512 removed.

## Why it is built this way

- **The planner goes through `chat`, the executor does not.** 11839cf8 (PR #1455) moved the planner LLM call into the normal `chat` pipeline. It gets streaming, fallback, history and billing for free, so `prepare_planner_request` only changes the request in place. Action requests need a long-lived SSE stream across many chat calls, so they leave `chat` early through `handle_todo_mode`.
- **Per-task overrides go inside the bridge config.** `chat_multiple_agents` does `primary_body.update(primary_config)`, which overwrites `body.configuration`. So worker and synthesizer overrides (prompt, tools, `response_type`, `stream`) are written into a deep copy of `bridge_configurations[agent]`. The comment in `_execute_single_task` explains it.
- **`skip_history` for main-agent tasks.** One user turn should make one history row. Main-agent workers skip their own rows, and `publish_plan_history_update` adds their summed telemetry to the planning row afterwards (6143f46a #1451, 6b71bf64). `_merge_task_metrics` explains why the numbers come from the executor: `AiConfig`, fallback and latency never reach the SSE `done` event.
- **Wallet check on the worker fallback swap.** Billing gates the original model in the middleware and never runs again. Without `resolve_wallet_fallback_key`, a fallback could step outside the org's plan or reach a provider with the wrong platform key. This came in with the September 2026 billing work (merged to master in #1769, 38aacc53). The fallback is "an optimisation here", so a refusal keeps the original model instead of failing the task.
- **Workers ask the user, not the planner.** Workers used to return `needs_planner`. The executor then marked the task `needs_replan` and called `_trigger_replan` mid-run. #1468 (4db41dd5, "improved todo and shift worker prompt in db", reached master in #1492) maps `needs_planner` to `waiting_for_user` and no longer fills `replan_queue`. `_trigger_replan`, `REPLAN_PRESERVE_STATUSES` and the `needs_replan` check in `_is_plan_blocked` are left over and can't be reached.
- **The plan shape follows the agent prompt.** #1512 (015f8dac, 2026-05-04) removed the built-in planner prompt and its fixed `tasks` dict schema. It moved tasks to `plan.plan.tasks` (a list) and removed the merge guard in `_save_plan_from_result`. No reason is recorded.
- A later refactor that splits `executor_service.py` into an `executor` package (332ed3b6 and others, May–June 2026) exists in the repo history but is **not** on master.

> [!TODO] Needs a human: #1512 made the agent's own prompt the whole planner prompt, and made `_get_planner_prompt_from_db` return the empty default even when `preBuiltPrompts.planner_prompt` exists. Is the DB planner prompt retired on purpose? If so, should the dead lookup, `session_memory` and the `_extract_*_answer_pairs` helpers go?

> [!TODO] Needs a human: `prepare_planner_request` rewrites `parsed_data["user"]` (old `questions` + "User Message: …"), but `chat` built `params["user"]` in `build_service_params` before that, and providers read `self.user`. So the rewritten message never reaches the planner LLM; it only shows up in history. Was it meant to?

> [!TODO] Needs a human: main-agent workers and the synthesizer run with `skip_history=True`. `process_background_tasks` returns before `compute_billing_events`, and `publish_plan_history_update` carries no billing events. Are wallet-billed plan executions charged anywhere?

> [!TODO] Needs a human: should `waiting_for_user` stay in `TERMINAL_STATUSES`? It has been there since the first commit (4a27cafc, #1430), and it lets a plan with an open question end as `completed`.

## Changing it safely

- **New `action`**: add a branch in `_stream_plan_action`. Every branch must end with `emit_done` or `emit_error`. Copy the approve branch's synthesize → `emit_done` → `publish_plan_history_update` order if it runs tasks.
- **New task field or status**: update `_get_runnable_tasks`, `_is_plan_blocked`, `_is_plan_complete` and `TERMINAL_STATUSES` together. Update the agent prompts that emit the plan, and the frontend that reads the plan JSON. The schema is not in this repo.
- **Worker prompt**: change `WORKER_SYSTEM_PROMPT_TEMPLATE` and the Mongo `worker_prompt` together. Mongo wins when it is set. Keep all six placeholders, and restart to pick up a Mongo change.
- **Metrics**: edit the **second** copy of the metric helpers, or delete the first copy.
- **Overrides for worker or synthesizer calls**: write them into the deep-copied `bridge_configurations[agent]` entry, not into the request body.
- **Check by hand** (there are no tests for this folder): a planner call with `"mode":"plan"` and a `thread_id`, then `GET` the `nd_plan_…` key in Redis, then `"action":"approve"` and watch the SSE stream, then `"action":"status"`. See [chat-completion flow](../flows/chat-completion.md) for the request setup.
