import json

from langchain_core.messages import HumanMessage, SystemMessage

from workflow.llm import create_llm, extract_text_from_response
from workflow.prompts import FINAL_ANSWER_PROMPT


def make_synthesizer_node():
    async def synthesizer_node(state: dict) -> dict:
        config = state.get("user_config") or {}
        completed = state.get("completed_tasks") or []
        if not completed:
            return {"final_answer": "No steps were completed successfully."}

        step_results = "\n\n".join(f"### {item['title']}\n{item['result']}" for item in completed)
        response_schema = state.get("response_schema")

        # Build format instruction based on response_schema
        if response_schema:
            format_instruction = (
                f"## Output format\n"
                f"The user expects a structured JSON response. Produce valid JSON matching this schema:\n"
                f"```json\n{json.dumps(response_schema, indent=2)}\n```\n"
                f"Return ONLY the JSON object — no markdown, no explanation."
            )
        else:
            format_instruction = "Respond in clear, well-formatted text."

        agent_persona = config.get("system_prompt", "")
        if agent_persona:
            prompt = (
                f"{agent_persona}\n\n"
                f"The user's goal was: \"{state['goal']}\"\n\n"
                f"Step results:\n{step_results}\n\n"
                f"{format_instruction}\n\n"
                f"Produce the FINAL consolidated output — the actual deliverable, not a summary."
            )
        else:
            prompt = FINAL_ANSWER_PROMPT.format(
                goal=state["goal"],
                step_results=step_results,
                format_instruction=format_instruction,
            )

        llm = create_llm(
            model=config.get("synthesizer_model", "gpt-4o-mini"),
            api_key=state["api_key"],
            temperature=config.get("planner_temperature", 0.4),
            streaming=True,
            json_mode=bool(response_schema),
            service=config.get("synthesizer_service", "openai"),
        )
        full_text = ""
        async for chunk in llm.astream([
            SystemMessage(content=prompt),
            HumanMessage(content="Produce the final consolidated output now."),
        ]):
            # Safely extract text from chunk, handling Anthropic content blocks
            c = chunk.content
            if isinstance(c, str):
                full_text += c
            elif isinstance(c, list):
                # Anthropic returns list of content blocks; extract text blocks only
                for block in c:
                    if isinstance(block, dict) and block.get("type") == "text":
                        full_text += block.get("text", "")

        return {"final_answer": full_text}

    return synthesizer_node
