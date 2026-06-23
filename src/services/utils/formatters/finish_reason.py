"""Maps each provider's raw finish/stop reason to the AI-middleware vocabulary.

Shared by every per-service response formatter so a single place defines how
provider-specific stop reasons (stop / end_turn / length / tool_calls / ...)
normalize to: completed | truncated | tool_call | other.
"""


def finish_reason_mapping(finish_reason):
    mapping = {
        # Completed / natural stop
        "stop": "completed",  # openai #open_router #gemini
        "end_turn": "completed",  # anthropic
        "completed": "completed",  # openai_response
        # Truncation due to token limits
        "length": "truncated",  # openai #open_router #gemini
        "max_tokens": "truncated",  # anthropic
        "max_output_tokens": "truncated",  # openai_response
        # Tool / function invocation
        "tool_calls": "tool_call",  # openai #gemini
        "tool_use": "tool_call",  # anthropic
        # Failed / errored generation
        "failed": "other",  # openai_response (response.failed)
    }
    return mapping.get(finish_reason, "other")
