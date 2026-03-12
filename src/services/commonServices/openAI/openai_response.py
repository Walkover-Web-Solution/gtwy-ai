import copy
import json
import re

from src.configs.constant import service_name
from src.configs.model_configuration import model_config_document
from src.services.utils.ai_middleware_format import Response_formatter

from ..baseService.baseService import BaseService
from ..createConversations import ConversationService

PLANNER_SYSTEM_PROMPT = """Analyze the user's message and decide the best execution strategy.

Respond with JSON in one of three modes:

MODE 1 — DIRECT (simple messages, greetings, straightforward questions, or anything a single response can handle):
{"mode": "direct"}

MODE 2 — QUESTION (ambiguous or vague request that needs clarification before proceeding):
{"mode": "question", "question": {"text": "Your clarifying question", "options": ["Option A", "Option B", "Option C"]}}

MODE 3 — TASKS (complex request that benefits from step-by-step execution):
{"mode": "tasks", "tasks": [{"title": "Step title", "description": "What to do in this step"}]}

Rules:
- Use QUESTION only when the request is genuinely ambiguous and could go in very different directions. Provide 2-4 short, specific options.
- Use TASKS when the request is complex enough that breaking it into steps produces better results. Keep tasks between 2-8 depending on complexity.
- Use DIRECT for everything else — greetings, simple questions, single-step tasks, or when a single coherent response suffices.
- If the user already answered a question (message contains "Selected: ..."), use TASKS.
- When in doubt, prefer DIRECT over TASKS."""


def _sse_event(event, data):
    return {"event": event, "data": json.dumps(data)}


class OpenaiResponse(BaseService):
    def _build_input_config(self):
        """Build the input configuration (shared between execute and execute_stream)."""
        conversation = ConversationService.createOpenAiConversation(
            self.configuration.get("conversation"), self.memory, self.files
        ).get("messages", [])
        developer = (
            [{"role": "developer", "content": self.configuration["prompt"]}] if not self.reasoning_model else []
        )

        if self.image_data and isinstance(self.image_data, list):
            self.customConfig["input"] = developer + conversation
            image_content = [{"type": "input_image", "image_url": url} for url in self.image_data]
            content = [{"type": "input_text", "text": self.user}] + image_content if self.user else image_content
            self.customConfig["input"].append({"role": "user", "content": content})
        elif self.files and len(self.files) > 0:
            self.customConfig["input"] = developer + conversation
            file_content = [{"type": "input_file", "file_url": file_url} for file_url in self.files]
            content = [{"type": "input_text", "text": self.user}] + file_content if self.user else file_content
            self.customConfig["input"].append({"role": "user", "content": content})
        else:
            user = [{"role": "user", "content": self.user}] if self.user else []
            self.customConfig["input"] = developer + conversation + user

        self.customConfig = self.service_formatter(self.customConfig, service_name["openai"])

        if "tools" not in self.customConfig and "parallel_tool_calls" in self.customConfig:
            del self.customConfig["parallel_tool_calls"]

        if len(self.built_in_tools) > 0:
            if (
                "web_search" in self.built_in_tools
                and "tools" in model_config_document[self.service][self.model]["configuration"]
            ):
                if "tools" not in self.customConfig:
                    self.customConfig["tools"] = []

                if self.web_search_filters and isinstance(self.web_search_filters, list):
                    web_search_tool = {
                        "type": "web_search",
                        "filters": {"allowed_domains": self.web_search_filters},
                    }
                else:
                    web_search_tool = {"type": "web_search_preview"}

                self.customConfig["tools"].append(web_search_tool)

    async def _stream_direct(self):
        """Normal streaming with full tool-call loop (direct mode)."""
        loop_count = 0
        tools = {}
        transfer_config = None
        model_response = None

        while True:
            model_response = None

            async for event in self.chats_stream(self.customConfig, self.apikey, service_name["openai"]):
                event_name = event.get("event", "")

                if event_name == "response.completed":
                    event_data = json.loads(event.get("data", "{}"))
                    if event_data.get("success"):
                        model_response = event_data["response"]
                    else:
                        yield _sse_event("error", {"error": event_data.get("error", "Unknown error")})
                        return
                elif event_name == "error":
                    yield event
                    return
                else:
                    yield event

            if model_response is None:
                yield _sse_event("error", {"error": "Stream completed without final response"})
                return

            has_function_call = any(
                output.get("type") == "function_call" for output in model_response.get("output", [])
            )

            if not has_function_call or loop_count > int(self.tool_call_count or 0):
                break

            loop_count += 1

            if self.customConfig.get("tool_choice") is not None and self.customConfig["tool_choice"] not in ["auto", "none"]:
                self.customConfig["tool_choice"] = "auto"

            function_calls = [
                output for output in model_response.get("output", [])
                if output.get("type") == "function_call"
            ]
            yield _sse_event("tool_calls", {
                "tools": [
                    {"name": fc.get("name"), "call_id": fc.get("call_id"), "arguments": fc.get("arguments", "")}
                    for fc in function_calls
                ]
            })

            func_response_data, mapping_response_data, tools_call_data = await self.run_tool(
                model_response, service_name["openai"]
            )
            self.func_tool_call_data.append(tools_call_data)

            if isinstance(tools_call_data, dict) and "transfer_agent_config" in tools_call_data:
                transfer_config = tools_call_data["transfer_agent_config"]
                yield _sse_event("agent_transfer", transfer_config)
                break

            for resp in func_response_data:
                yield _sse_event("tool_result", {
                    "name": resp.get("name"),
                    "tool_call_id": resp.get("tool_call_id"),
                    "content": resp.get("content"),
                })

            self.customConfig, tools = self.update_configration(
                model_response, func_response_data, self.customConfig,
                mapping_response_data, service_name["openai"], tools,
            )

            yield _sse_event("activity", {"message": "Continuing AI reasoning with tool results…"})

        if tools:
            response = await Response_formatter(
                model_response, service_name["openai"], tools, self.type, self.image_data
            )
        else:
            response = await Response_formatter(
                model_response, service_name["openai"], {}, self.type, self.image_data
            )

        historyParams = {}
        if not self.playground:
            historyParams = self.prepare_history_params(response, model_response, tools, transfer_config)

        result = {"success": True, "modelResponse": model_response, "historyParams": historyParams, "response": response}
        if transfer_config:
            result["transfer_agent_config"] = transfer_config

        yield _sse_event("done", result)

    async def _execute_tasks_stream(self, tasks):
        """Execute planned subtasks sequentially with streaming, then synthesize final answer."""
        completed_context = []

        for task in tasks:
            yield _sse_event("task_start", {"task_id": task["id"], "title": task["title"]})

            try:
                task_config = copy.deepcopy(self.customConfig)
                developer_content = self.configuration.get("prompt", "")

                task_config["input"] = [
                    {"role": "developer", "content": f"{developer_content}\n\nOverall goal: {self.user}\nExecute this subtask thoroughly. Be specific and produce real output."},
                ]

                if completed_context:
                    context_summary = "\n".join([
                        f"Step '{c['title']}': {c['result']}" for c in completed_context
                    ])
                    task_config["input"].append({
                        "role": "assistant",
                        "content": f"Previously completed steps:\n{context_summary}",
                    })

                task_config["input"].append({
                    "role": "user",
                    "content": f"Subtask: {task['title']}\nDescription: {task['description']}",
                })

                full_result = ""
                task_model_response = None
                task_failed = False

                async for event in self.chats_stream(task_config, self.apikey, service_name["openai"]):
                    event_name = event.get("event", "")

                    if event_name == "delta":
                        event_data = json.loads(event.get("data", "{}"))
                        chunk = event_data.get("chunk", "")
                        full_result += chunk
                        yield _sse_event("task_progress", {"task_id": task["id"], "chunk": chunk})
                    elif event_name == "thinking":
                        yield event
                    elif event_name == "response.completed":
                        event_data = json.loads(event.get("data", "{}"))
                        if event_data.get("success"):
                            task_model_response = event_data["response"]
                    elif event_name == "error":
                        yield _sse_event("task_failed", {
                            "task_id": task["id"],
                            "error": json.loads(event.get("data", "{}")).get("error", "Unknown error"),
                        })
                        task_failed = True
                        break

                if not task_failed:
                    completed_context.append({"title": task["title"], "result": full_result})
                    yield _sse_event("task_done", {"task_id": task["id"], "result": full_result})

            except Exception as e:
                yield _sse_event("task_failed", {"task_id": task["id"], "error": str(e)})

        # Final synthesis — combine all step results into one deliverable
        if completed_context:
            yield _sse_event("final_answer_start", {"message": "Preparing final output..."})

            step_results = "\n\n".join([
                f"### {c['title']}\n{c['result']}" for c in completed_context
            ])

            synthesis_config = copy.deepcopy(self.customConfig)
            synthesis_config["input"] = [
                {
                    "role": "developer",
                    "content": (
                        f'You were given this goal: "{self.user}"\n\n'
                        f"You completed it step by step. Here are the results:\n\n{step_results}\n\n"
                        "Produce the FINAL consolidated output — not a summary of steps, but the real deliverable."
                    ),
                },
                {"role": "user", "content": "Produce the final consolidated output now."},
            ]

            final_text = ""
            final_model_response = None

            async for event in self.chats_stream(synthesis_config, self.apikey, service_name["openai"]):
                event_name = event.get("event", "")

                if event_name == "delta":
                    event_data = json.loads(event.get("data", "{}"))
                    chunk = event_data.get("chunk", "")
                    final_text += chunk
                    yield _sse_event("final_answer_progress", {"chunk": chunk})
                elif event_name == "response.completed":
                    event_data = json.loads(event.get("data", "{}"))
                    if event_data.get("success"):
                        final_model_response = event_data["response"]

            if final_model_response:
                response = await Response_formatter(
                    final_model_response, service_name["openai"], {}, self.type, self.image_data
                )
            else:
                response = {"data": {"content": final_text}, "usage": {}}

            historyParams = {}
            if not self.playground:
                historyParams = self.prepare_history_params(response, final_model_response or {}, {}, None)

            yield _sse_event("done", {
                "success": True,
                "modelResponse": final_model_response or {},
                "historyParams": historyParams,
                "response": response,
            })
        else:
            yield _sse_event("done", {
                "success": True,
                "modelResponse": {},
                "historyParams": {},
                "response": {"data": {"content": "No steps were completed successfully."}, "usage": {}},
            })

    async def execute_stream(self):
        """
        Async generator — orchestrates the streaming flow:
        1. Planning phase: uses the SAME agent with planning instructions to decide strategy
        2. Streams the plan as chunks to the client
        3. Routes to the appropriate handler based on the plan mode
        """
        if self.type == "image":
            raise ValueError("Streaming is not supported for image generation")

        self._build_input_config()

        # Phase 1: Planning — inject planning instructions into the same agent
        plan_config = copy.deepcopy(self.customConfig)
        plan_config["input"] = [
            {"role": "developer", "content": PLANNER_SYSTEM_PROMPT},
            {"role": "user", "content": self.user or ""},
        ]
        # Strip tools for planning — we only need a JSON decision
        plan_config.pop("tools", None)
        plan_config.pop("tool_choice", None)
        plan_config.pop("parallel_tool_calls", None)

        plan_text = ""
        yield _sse_event("planning", {"message": "Analyzing your request..."})

        async for event in self.chats_stream(plan_config, self.apikey, service_name["openai"]):
            event_name = event.get("event", "")
            if event_name == "delta":
                event_data = json.loads(event.get("data", "{}"))
                chunk = event_data.get("chunk", "")
                plan_text += chunk
                yield _sse_event("planning", {"chunk": chunk})
            elif event_name == "thinking":
                yield _sse_event("planning_thinking", json.loads(event.get("data", "{}")))
            elif event_name == "response.completed":
                pass  # we use accumulated plan_text
            elif event_name == "error":
                plan_text = '{"mode": "direct"}'
                break

        # Parse plan JSON from the agent's streamed output
        try:
            plan = json.loads(plan_text.strip())
        except json.JSONDecodeError:
            match = re.search(r"\{.*\}", plan_text, re.DOTALL)
            if match:
                try:
                    plan = json.loads(match.group(0))
                except json.JSONDecodeError:
                    plan = {"mode": "direct"}
            else:
                plan = {"mode": "direct"}

        mode = plan.get("mode", "direct")

        # Mode: question — need clarification from the user
        if mode == "question" and plan.get("question"):
            yield _sse_event("question", {
                "text": plan["question"].get("text", ""),
                "options": plan["question"].get("options", []),
            })
            yield _sse_event("done", {
                "success": True,
                "mode": "question",
                "modelResponse": {},
                "historyParams": {},
                "response": {"data": {"content": ""}, "usage": {}},
            })
            return

        # Mode: tasks — decompose and execute step by step
        if mode == "tasks" and plan.get("tasks"):
            tasks = []
            for i, task in enumerate(plan["tasks"]):
                tasks.append({
                    "id": str(i + 1),
                    "title": task.get("title", ""),
                    "description": task.get("description", ""),
                    "status": "pending",
                })
            yield _sse_event("plan_ready", {
                "tasks": [
                    {"id": t["id"], "title": t["title"], "description": t["description"], "status": t["status"]}
                    for t in tasks
                ],
            })
            async for event in self._execute_tasks_stream(tasks):
                yield event
            return

        # Mode: direct (default) — normal streaming with tool-call loop
        async for event in self._stream_direct():
            yield event

    async def execute(self):
        historyParams = {}
        tools = {}
        functionCallRes = {}
        if self.type == "image":
            self.customConfig["prompt"] = self.user
            openAIResponse = await self.image(self.customConfig, self.apikey, service_name["openai"])
            modelResponse = openAIResponse.get("modelResponse", {})
            if not openAIResponse.get("success"):
                if not self.playground:
                    await self.handle_failure(openAIResponse)
                raise ValueError(openAIResponse.get("error"))
            response = await Response_formatter(
                modelResponse, service_name["openai"], tools, self.type, self.image_data
            )
            if not self.playground:
                historyParams = self.prepare_history_params(response, modelResponse, tools, None)
                historyParams["message"] = "image generated successfully"
                historyParams["type"] = "assistant"
        else:
            self._build_input_config()

            openAIResponse = await self.chats(self.customConfig, self.apikey, service_name["openai"])
            modelResponse = openAIResponse.get("modelResponse", {})

            if not openAIResponse.get("success"):
                if not self.playground:
                    await self.handle_failure(openAIResponse)
                raise ValueError(openAIResponse.get("error"))

            # Check for function calls in multiple possible locations with fallback
            has_function_call = (
                any(output.get("type") == "function_call" for output in modelResponse.get("output", []))
                or any(output.get("type") == "tool_call" for output in modelResponse.get("output", []))
                or any(
                    "function_call" in str(output)
                    for output in modelResponse.get("output", [])
                    if output.get("type") in ["reasoning", "message", "output_text"]
                )
            )

            if has_function_call:
                functionCallRes = await self.function_call(
                    self.customConfig, service_name["openai"], openAIResponse, 0, {}
                )
                if not functionCallRes.get("success"):
                    await self.handle_failure(functionCallRes)
                    raise ValueError(functionCallRes.get("error"))
                self.update_model_response(modelResponse, functionCallRes)
                response = await Response_formatter(
                    functionCallRes.get("modelResponse", {}),
                    service_name["openai"],
                    functionCallRes.get("tools", {}),
                    self.type,
                    self.image_data,
                )
                tools = functionCallRes.get("tools", {})
            else:
                response = await Response_formatter(
                    modelResponse, service_name["openai"], {}, self.type, self.image_data
                )

            if not self.playground:
                transfer_config = (
                    functionCallRes.get("transfer_agent_config") if has_function_call and functionCallRes else None
                )
                historyParams = self.prepare_history_params(response, modelResponse, tools, transfer_config)

        # Add transfer_agent_config to return if transfer was detected
        result = {"success": True, "modelResponse": modelResponse, "historyParams": historyParams, "response": response}
        if functionCallRes.get("transfer_agent_config"):
            result["transfer_agent_config"] = functionCallRes["transfer_agent_config"]
        return result
