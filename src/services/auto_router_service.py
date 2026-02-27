from config import Config
from notdiamond import NotDiamond
from src.configs.model_configuration import model_config_document

client = NotDiamond(api_key=Config.NOTDIAMOND_API_KEY)

async def find_best_model(parsed_data):
    service = parsed_data["service"]
    models = [
        m for m, v in model_config_document[service].items() 
        if isinstance(v, dict) and v.get("status") == 1 
        and v.get("validationConfig", {}).get("type") == "chat"
        and v.get("validationConfig", {}).get("auto_router_support", False)
    ]

    result = client.model_router.select_model(
        messages=[
            {"role": "system", "content": parsed_data["configuration"]["prompt"]},
            {"role": "user", "content": parsed_data["user"]}
        ],
        llm_providers=[
            {"provider": service, "model": model } for model in models
        ],
        tradeoff="cost"
    )

    return result.providers[0].model 