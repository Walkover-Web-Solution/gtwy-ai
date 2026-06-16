

api_key_status = {
    "working": "working",
    "invalid": "invalid",
    "unauthorized": "unauthorized",
    "limited": "exhausted",
    "service_down": "service_down"
}

service_name = {
    "openai": "openai",
    "gemini": "gemini",
    "anthropic": "anthropic",
    "grok": "grok",
    "deepseek": "deepseek",
    "groq": "groq",
    "open_router": "open_router",
    "mistral": "mistral",
    "deepgram": "deepgram",
    "openai_completion": "openai_completion",
    "neev_cloud": "neev_cloud",
    "moonshot": "moonshot",
}

bridge_ids = {
    "gpt_memory": "6752d9fc232e8659b2b65f0d",
    "suggest_model": "67a75ab42d85a6d4f16a4c7e",
    "make_question": "67459164ea7147ad4b75f92a",
    "optimze_prompt": "6843d832aab19264b8967f3b",
    "create_bridge_using_ai": "67e4e7934e58b9c3b991a29c",
    "structured_output_optimizer": "67766c4eec020b944b3e0670",
    "chatbot_response_with_actions": "67b3157bdd16f681b71b06a4",
    "chatbot_response_without_actions": "67b30d46f8ab2d672f1682b4",
    "chatbot_suggestions": "674710c9141fcdaeb820aeb8",
    "generate_summary": "679ca9520a9b42277fd2a3c1",
    "function_agrs_using_ai": "67c81a424f3136bfb0e81906",
    "compare_result": "67ce993c8407023ad4f7b277",
    "generate_description": "6800d48f7dfc8ddcc495f918",
    "improve_prompt_optimizer": "68e4ac02739a8b89ba27b22a",
    "generate_test_cases": "68e8d1fbf8c9ba2043cf7afd",
    "canonicalizer": "6973200cf60dd5bf64eeb325", 
    "query_refiner": "69ae598263c3cc88af31170b",
}

__all__ = ["service_name", "bridge_ids"]

prebuilt_prompt_bridge_id = [
    "optimze_prompt",
    "gpt_memory",
    "structured_output_optimizer",
    "chatbot_suggestions",
    "generate_summary",
    "generate_test_cases",
]

# cd_ = can delete (DB-backed caches, safe to regenerate)
# nd_ = no delete (Redis-ONLY stores or cost/metrics accumulators)
redis_keys = {
    # Deletable — regenerable from DB
    "get_bridge_data_": "cd_get_bridge_data_",
    "bridge_data_with_tools_": "cd_bridge_data_with_tools_",
    "timezone_and_org_": "cd_timezone_and_org_",
    "conversation_": "cd_conversation_",
    "last_transffered_agent_": "cd_last_transffered_agent_",
    # Protected — source of truth or cost/metrics accumulators
    "bridgeusedcost_": "nd_bridgeusedcost_",
    "folderusedcost_": "nd_folderusedcost_",
    "apikeyusedcost_": "nd_apikeyusedcost_",
    "apikeylastused_": "nd_apikeylastused_",
    "bridgelastused_": "nd_bridgelastused_",
    "files_": "nd_files_",
    "gpt_memory_": "nd_gpt_memory_",
    "gpt_memory_counter_": "nd_gpt_memory_counter_",
    "metrix_bridges_": "nd_metrix_bridges_",
    "rate_limit_": "nd_rate_limit_",
    "batch_": "nd_batch_",
    "plan_": "nd_plan_",
    # Usage-alert feature: per-day spend buckets + once-per-period de-dupe markers
    "dailyusedcost_": "nd_dailyusedcost_",
    "usagealertsent_": "nd_usagealertsent_",
    "usagespikealert_": "nd_usagespikealert_",
}

tag_keys = {
    "agent": "tag:agent:",
    "version": "tag:version:",
    "tool": "tag:tool:",
    "apikey": "tag:apikey:",
    "folder": "tag:folder:",
    "connected_agent": "tag:connected_agent:",
    "wrapper": "tag:wrapper:",
    "rag": "tag:rag:",
}

limit_types = {"bridge": "bridge", "folder": "folder", "apikey": "apikey"}

new_agent_service = {
    "openai": "gpt-4o",
    "anthropic": "claude-3-7-sonnet-latest",
    "groq": "llama-3.3-70b-versatile",
    "open_router": "deepseek/deepseek-chat-v3-0324:free",
    "mistral": "mistral-medium-latest",
    "gemini": "gemini-2.5-flash",
    "grok": "grok-4-fast",
    "deepseek": "deepseek-v4-flash",
    "deepgram": "nova-3",
    "neev_cloud": "gpt-oss-120b",
    "moonshot": "kimi-k2.6",
}

inbuild_tools = {"Gtwy_Web_Search": "Gtwy_Web_Search"}

VALID_RESPONSE_TYPES = {"text", "json_object", "json_schema"}

GPT_MEMORY_TURNS_PER_CYCLE = 3

agent_config_update_keys = {
    "_response_type": "_response_type",
    "_user_message": "_user_message"
}

alert_types = {
    "error": "Error",
    "variable": "Variable",
    "metrix_limit_reached": "metrix_limit_reached",
    "retry_mechanism": "retry_mechanism",
    "broadcast_response": "broadcast_response",
}

# Usage-alert feature -------------------------------------------------------
# Mail-type enum sent in the body of the mail API. The string values are the
# enum the mail API expects — swap them for the exact values from the mail API
# spec when it is provided.
usage_mail_types = {
    "threshold": "USAGE_THRESHOLD_REACHED",
    "spike": "USAGE_DAILY_SPIKE",
    "limit_reached": "USAGE_LIMIT_REACHED",
}

# Endpoint for the mail API that sends the usage-alert emails.
USAGE_ALERT_MAIL_URL = "https://flow.sokt.io/func/scrikY1L98L6"

# Tunable defaults for the usage-alert checks.
usage_alert_config = {
    "threshold_percent": 0.8,       # fire the threshold email at 80% of the limit
    "spike_multiplier": 3.0,        # a day is a "spike" when it exceeds N x the trailing average
    "spike_window_days": 7,         # trailing window used to compute the average daily spend
    "spike_min_history_days": 3,    # require this many past days of data before spike can fire
    "daily_bucket_ttl_days": 8,     # how long each per-day bucket lives (window + buffer)
}

auto_model_tradeoff = {
    "quality": None,
    "cost": "cost",
    "speed": "latency"
}