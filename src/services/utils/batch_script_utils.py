# Import service-specific batch handlers
from ..commonServices.anthropic.anthropic_run_batch import handle_batch_results as anthropic_handle_batch
from ..commonServices.Google.gemini_run_batch import handle_batch_results as gemini_handle_batch
from ..commonServices.groq.groq_run_batch import handle_batch_results as groq_handle_batch
from ..commonServices.Mistral.mistral_run_batch import handle_batch_results as mistral_handle_batch
from ..commonServices.openAI.openai_run_batch import handle_batch_results as openai_handle_batch

BATCH_RESULT_HANDLERS = {
    "gemini": gemini_handle_batch,
    "anthropic": anthropic_handle_batch,
    "openai": openai_handle_batch,
    "groq": groq_handle_batch,
    "mistral": mistral_handle_batch,
}


def get_batch_result_handler(service):
    return BATCH_RESULT_HANDLERS.get(service)


def is_finalized_batch_item(item):
    """
    Returns True when a batch item is final and safe to send in webhook.
    """
    status_code = item.get("status_code")
    if status_code is not None and status_code >= 400:
        return True

    if item.get("error"):
        return True

    data = item.get("data") or {}
    content = data.get("content")
    finish_reason = data.get("finish_reason")

    return content is not None and finish_reason in {"completed", "truncated", "tool_call"}
