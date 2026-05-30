import asyncio

from globals import logger
from src.services.prebuilt_prompt_service import get_multiple_prebuilt_prompts_without_org_service
from src.services.todo.executor.constants import WORKER_SYSTEM_PROMPT_TEMPLATE

_worker_template_cache: str | None = None
_worker_template_fetched: bool = False
_worker_template_lock = asyncio.Lock()
_synthesizer_prompt_cache: str | None = None


async def get_synthesizer_prompt() -> str:
    """Return cached synthesizer prompt. Populated on first worker-prompt fetch."""
    return _synthesizer_prompt_cache or ""


async def get_worker_prompt_template() -> str:
    """Return the worker prompt template, fetching both worker + synthesizer from DB once."""
    global _worker_template_cache, _worker_template_fetched, _synthesizer_prompt_cache

    if _worker_template_fetched:
        return _worker_template_cache or WORKER_SYSTEM_PROMPT_TEMPLATE

    async with _worker_template_lock:
        if _worker_template_fetched:
            return _worker_template_cache or WORKER_SYSTEM_PROMPT_TEMPLATE

        try:
            prompt_data = await get_multiple_prebuilt_prompts_without_org_service(
                ["worker_prompt", "synthesizer_prompt"]
            )
            worker_override = prompt_data.get("worker_prompt")
            synth_override = prompt_data.get("synthesizer_prompt")
            if isinstance(worker_override, str) and worker_override.strip():
                _worker_template_cache = worker_override
            if isinstance(synth_override, str) and synth_override.strip():
                _synthesizer_prompt_cache = synth_override
        except Exception as err:
            logger.error(f"Error fetching prompts from preBuiltPrompts: {err}")
        finally:
            _worker_template_fetched = True

    return _worker_template_cache or WORKER_SYSTEM_PROMPT_TEMPLATE
