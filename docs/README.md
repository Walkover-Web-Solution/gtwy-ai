---
verified_at: 6fdb8181
verified_on: 2026-10-13
---
# Docs index

Written for AI coding agents and new engineers. Every path, symbol, route and env var in these docs was checked against the code at the commit in that doc's front matter. Run `/ai-docs check` to see what has drifted since then, and `/ai-docs update` to fix it.

Start with [AGENTS.md](../AGENTS.md), which has the commands and the rules, then [architecture.md](architecture.md).

## System

| Doc | Read it when |
|---|---|
| [architecture.md](architecture.md) | First, for any change: layers, external systems, contracts with gtwy-node |
| [conventions.md](conventions.md) | Writing code: where files go, layer rules, errors, Redis/queue rules, spellings you must not fix |
| [recipes.md](recipes.md) | Adding a provider, route, tool, env var or Redis key |
| [glossary.md](glossary.md) | A term reads oddly (bridge, wallet, hold, `cd_`/`nd_`, wire_format) |
| [environment.md](environment.md) | Running locally, or adding or changing an env var |
| [api.md](api.md) | Adding or changing a route; checking auth and error shapes |
| [data-model.md](data-model.md) | Touching Mongo, Postgres, Timescale, Redis keys or queues |

## Flows

| Doc | Read it when |
|---|---|
| [flows/chat-completion.md](flows/chat-completion.md) | Changing anything on the main request path (`/chat/completion`, `/ai/completion`, `/openai/responses`) |
| [flows/startup-and-background-jobs.md](flows/startup-and-background-jobs.md) | Changing startup, adding a background loop, or debugging a pod drain |

## Modules

| Doc | Read it when changing |
|---|---|
| [modules/agent-configuration.md](modules/agent-configuration.md) | Auth, rate limits, how an agent's config is loaded and merged, the direct agent |
| [modules/chat-orchestration.md](modules/chat-orchestration.md) | `common.py` / `common_utils.py`: pre-tools, fallback, streaming, transfer, caching, memory |
| [modules/tool-calling.md](modules/tool-calling.md) | The tool loop, HTTP tools, agent-as-tool, MCP, web search |
| [modules/llm-providers.md](modules/llm-providers.md) | Providers, the services/models registries, formatters, token cost, auto router |
| [modules/billing-and-limits.md](modules/billing-and-limits.md) | Wallet, credit holds, plans, spend limits, usage alerts |
| [modules/batch-api.md](modules/batch-api.md) | Provider batch submit and the result poller |
| [modules/todo-mode.md](modules/todo-mode.md) | Plan mode: planner, executor, synthesizer |
| [modules/reviewer-and-guardrails.md](modules/reviewer-and-guardrails.md) | The reviewer loop; the (unwired) guardrails validator |
| [modules/gtwy-browser.md](modules/gtwy-browser.md) | The Gtwy_Browser tool, Steel, cookies, live view |
| [modules/queues-history-metrics.md](modules/queues-history-metrics.md) | RabbitMQ, history and metrics payloads, Redis helpers, alerts |
| [modules/testcases.md](modules/testcases.md) | The testcases and rerun product features |
| [modules/media-rag-chatbot.md](modules/media-rag-chatbot.md) | File uploads, RAG query, the chatbot endpoint |

## Needs a human

The docs mark about 40 open questions as "Needs a human". Most of them are likely bugs found while documenting; none of them was fixed. These have the most impact:

**Security**
- File upload routes have no auth and accept any content type into the public bucket. See [media-rag-chatbot.md](modules/media-rag-chatbot.md#gotchas).
- The RAG query does not scope by the caller's org, so any valid JWT can search any resource. See [media-rag-chatbot.md](modules/media-rag-chatbot.md#rag-query).

**Money**
- Batch traffic on the platform key is never charged. See [batch-api.md](modules/batch-api.md).
- Reviewer calls, and possibly plan-mode sub-tasks, are never billed. See [reviewer-and-guardrails.md](modules/reviewer-and-guardrails.md) and [todo-mode.md](modules/todo-mode.md).
- On the non-stream path, a second `update_usage_metrics` overwrites the summed cost after a review. See [reviewer-and-guardrails.md](modules/reviewer-and-guardrails.md).
- Credit holds that outlive their token are never reconciled. See [billing-and-limits.md](modules/billing-and-limits.md#gotchas).

**Broken or surprising behaviour**
- A bare `raise` after a successful streamed transfer. See [chat-orchestration.md](modules/chat-orchestration.md).
- Guardrails are not wired in, although the old docs say they are. See [reviewer-and-guardrails.md](modules/reviewer-and-guardrails.md).
- Deepgram calls fail on every call, and the `openai_completion` service routing is unclear. See [llm-providers.md](modules/llm-providers.md).
- The frozen-tab repair for the browser tool is not merged. See [gtwy-browser.md](modules/gtwy-browser.md).
- Other agents' history rows are lost on a non-orchestrator transfer. See [queues-history-metrics.md](modules/queues-history-metrics.md).
- Which Mongo collection holds prebuilt prompts (`preBuiltPrompts` vs `prebuiltprompts`)? See [data-model.md](data-model.md).

Open questions per doc:
- [billing-and-limits.md](modules/billing-and-limits.md): 5
- [batch-api.md](modules/batch-api.md): 5
- [testcases.md](modules/testcases.md): 5
- [reviewer-and-guardrails.md](modules/reviewer-and-guardrails.md): 5
- [todo-mode.md](modules/todo-mode.md): 4
- [queues-history-metrics.md](modules/queues-history-metrics.md): 3
- [media-rag-chatbot.md](modules/media-rag-chatbot.md): 3
- [agent-configuration.md](modules/agent-configuration.md): 2
- [tool-calling.md](modules/tool-calling.md): 2
- [gtwy-browser.md](modules/gtwy-browser.md): 2
- [chat-orchestration.md](modules/chat-orchestration.md): 1
- [llm-providers.md](modules/llm-providers.md): 1
- [data-model.md](data-model.md): 1
- [flows/chat-completion.md](flows/chat-completion.md): 1

## Older docs

`docs/AI_INSTRUCTIONS.md`, `docs/CHAT_COMPLETION_FLOW.md` and `docs/GTWY_BROWSER.md` came before this set:
- Their still-true content moved into the docs above.
- The rest describes code that no longer exists: deleted providers, removed functions, and a guardrails step.

Prefer the docs above.
