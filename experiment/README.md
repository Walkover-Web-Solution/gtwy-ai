# Agentic AI Platform (Experiment)

A fully working multi-agent AI platform built with FastAPI + LangGraph + OpenAI.  
**No database required** — all data is stored in a single `data.json` file.

---

## What it does

- Create **Agents** with custom system prompts, models, and temperatures
- Create **Tools** (API call tools that POST to `flow.sokt.io`) with per-field AI/User source control
- Attach tools to agents — AI fills its fields, static values are injected automatically
- Run agents via a **chat UI** with real-time streaming (WebSocket)
- **Agent-to-Agent (A2A)** delegation — parent agents can call sub-agents
- Test tools directly from the UI before attaching them to an agent
- Edit agents and tools at any time from the UI

---

## Quick Start

### 1. Clone / copy the `experiment/` folder

```
experiment/
├── main.py
├── requirements.txt
├── .env.example
├── static/index.html
├── db/
├── graph/
├── routes/
├── schemas/
└── services/
```

### 2. Create your `.env` file

```bash
cp .env.example .env
```

Open `.env` and set your OpenAI API key:

```
OPENAI_API_KEY=sk-xxxxxxxxxxxxxxxxxxxxxxxx
```

> That's the **only** thing you need to set. No database, no Redis, nothing else.

### 3. Install dependencies

```bash
pip install -r requirements.txt
```

### 4. Run the server

```bash
python main.py
```

Server starts at **http://localhost:8888**

Open that URL in your browser — the full UI is there.

---

## How to use

### Create a Tool
1. Go to **Tools** tab → **+ Create Tool**
2. Set a name, description, Script ID (`flow.sokt.io/func/<script_id>`)
3. Add fields — for each field choose:
   - **🤖 AI fills** → the LLM decides this value at runtime
   - **👤 User** → you set a fixed static value now (API key, org ID, etc.)
4. Click **Create**

### Create an Agent
1. Go to **Agents** tab → **+ Create Agent**
2. Set name, system prompt, model (`gpt-4o-mini` / `gpt-4o`), temperature
3. Click **Create**

### Attach a Tool to an Agent
1. On the agent card → click **Edit**
2. In the **Attached Tools** section → select a tool → **+ Attach**

### Run an Agent
1. Go to **Chat** tab
2. Select your agent from the dropdown
3. Enter your OpenAI API key (or leave blank to use the `.env` key)
4. Type a goal and hit **Run**

### Test a Tool directly
1. Go to **Tools** tab
2. Click **▶ Test** on any `api_call` tool
3. Fill in the AI-source fields and click **▶ Run**
4. See the live response from `flow.sokt.io`

---

## Data storage

All agents, tools, sessions, and A2A registry are saved in:

```
experiment/data.json
```

This file is auto-created on first run. You can inspect or edit it directly.  
To reset everything, just delete `data.json` and restart.

---

## Tech stack

| Layer | Library |
|---|---|
| API server | FastAPI + Uvicorn |
| Agent graph | LangGraph (StateGraph) |
| LLM | LangChain OpenAI (ChatOpenAI) |
| Tool calls | aiohttp → `flow.sokt.io` |
| Storage | Plain JSON file (`data.json`) |
| UI | Vanilla HTML/CSS/JS (single file) |
| Streaming | WebSocket |

---

## Requirements

- Python 3.11+
- OpenAI API key
- Internet access (for `flow.sokt.io` tool calls)
