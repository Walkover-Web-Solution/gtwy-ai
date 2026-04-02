import json
from typing import Any

from langchain_openai import ChatOpenAI
from globals import logger


def create_llm(
    model: str,
    api_key: str,
    temperature: float = 0.3,
    streaming: bool = False,
    json_mode: bool = False,
    tools: list | None = None,
) -> ChatOpenAI:
    """Centralized LLM factory for all workflow nodes."""
    kwargs: dict[str, Any] = {}
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


async def safe_invoke(llm: ChatOpenAI, messages: list) -> tuple[Any, bool]:
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
