import json
import re
from typing import Any

from langchain_openai import ChatOpenAI
from langchain_anthropic import ChatAnthropic
from globals import logger


def create_llm(
    model: str,
    api_key: str,
    temperature: float = 0.3,
    streaming: bool = False,
    json_mode: bool = False,
    tools: list | None = None,
    service: str = "openai",
) -> ChatOpenAI | ChatAnthropic:
    """Centralized LLM factory for all workflow nodes."""
    kwargs: dict[str, Any] = {}
    
    if service.lower() == "anthropic" or "claude" in model.lower():
        # For Anthropic/Claude: json_mode is handled via prompt instruction, not model_kwargs.
        # Do NOT enable extended thinking for JSON mode — it returns content blocks that break json.loads().
        # Instead, instruct the model via system prompt to return valid JSON.
        llm = ChatAnthropic(
            model=model,
            api_key=api_key,
            temperature=temperature,
            streaming=streaming,
            **kwargs,
        )
    else:
        if json_mode:
            kwargs["model_kwargs"] = {"response_format": {"type": "json_object"}}

        llm = ChatOpenAI(
            model=model,
            api_key=api_key,
            temperature=temperature,
            streaming=streaming,
            **kwargs,
        )
    
    if tools:
        llm = llm.bind_tools(tools)
    return llm


def extract_text_from_response(response) -> str:
    """Extract text content from LLM response, handling both OpenAI and Anthropic formats.
    
    For Anthropic/Claude, response.content can be:
    - A string (normal case)
    - A list of content blocks like [{"type": "thinking", ...}, {"type": "text", "text": "..."}]
    
    This function safely extracts the text block.
    """
    content = response.content
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                return block.get("text", "")
        return "".join(str(b.get("text", "")) if isinstance(b, dict) else "" for b in content)
    return str(content)


def extract_json_from_text(text: str) -> dict:
    """Robustly extract JSON from LLM output that may contain markdown fences, 
    explanations, or thinking text around the JSON.
    
    Handles:
    - Clean JSON string
    - JSON wrapped in ```json ... ``` fences
    - JSON surrounded by explanation text
    - High-thinking model output with reasoning before/after JSON
    """
    text = text.strip()
    
    # 1. Try direct parse first
    try:
        return json.loads(text)
    except (json.JSONDecodeError, ValueError):
        pass
    
    # 2. Try extracting from markdown code fences
    fence_pattern = re.compile(r"```(?:json)?\s*\n?(.*?)\n?\s*```", re.DOTALL)
    matches = fence_pattern.findall(text)
    for match in matches:
        try:
            return json.loads(match.strip())
        except (json.JSONDecodeError, ValueError):
            continue
    
    # 3. Try finding the outermost JSON object in the text
    brace_start = text.find("{")
    if brace_start != -1:
        depth = 0
        for i in range(brace_start, len(text)):
            if text[i] == "{":
                depth += 1
            elif text[i] == "}":
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(text[brace_start:i + 1])
                    except (json.JSONDecodeError, ValueError):
                        break
    
    # 4. Final fallback — raise with helpful context
    raise ValueError(f"Could not extract valid JSON from LLM response: {text[:200]}...")


async def safe_invoke(llm: ChatOpenAI | ChatAnthropic, messages: list) -> tuple[Any, bool]:
    """Invoke LLM with structured error handling. Returns (response, success)."""
    try:
        response = await llm.ainvoke(messages)
        return response, True
    except json.JSONDecodeError as e:
        logger.error(f"LLM returned invalid JSON: {e}")
        return None, False
    except Exception as e:
        logger.error(f"LLM call failed: {e}")
        return None, False
