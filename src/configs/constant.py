

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
    "groq": "groq",
    "open_router": "open_router",
    "mistral": "mistral",
    "ai_ml": "ai_ml",
    "deepgram": "deepgram",
    "openai_completion": "openai_completion",
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

redis_keys = {
    "pdf_url_": "pdf_url_",
    "get_bridge_data_": "get_bridge_data_",
    "bridge_data_with_tools_": "bridge_data_with_tools_",
    "metrix_bridges_": "metrix_bridges_",
    "rate_limit_": "rate_limit_",
    "files_": "files_",
    "batch_": "batch_",
    "avg_response_time_": "avg_response_time_",
    "gpt_memory_": "gpt_memory_",
    "timezone_and_org_": "timezone_and_org_",
    "conversation_": "conversation_",
    "bridgelastused_": "bridgelastused_",
    "apikeylastused_": "apikeylastused_",
    "bridgeusedcost_": "bridgeusedcost_",
    "folderusedcost_": "folderusedcost_",
    "apikeyusedcost_": "apikeyusedcost_",
    "last_transffered_agent_": "last_transffered_agent_",
}

limit_types = {"bridge": "bridge", "folder": "folder", "apikey": "apikey"}

new_agent_service = {
    "openai": "gpt-4o",
    "anthropic": "claude-3-7-sonnet-latest",
    "groq": "llama-3.3-70b-versatile",
    "open_router": "deepseek/deepseek-chat-v3-0324:free",
    "mistral": "mistral-medium-latest",
    "gemini": "gemini-2.5-flash",
    "ai_ml": "gpt-oss-20b",
    "grok": "grok-4-fast",
    "deepgram": "nova-3",
}

inbuild_tools = {"Gtwy_Web_Search": "Gtwy_Web_Search"}

VALID_RESPONSE_TYPES = {"text", "json_object", "json_schema"}

agent_config_update_keys = {
    "_response_type": "_response_type",
    "_user_message": "_user_message"
}
