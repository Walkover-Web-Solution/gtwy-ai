from config import Config
from notdiamond import AsyncNotDiamond
from globals import logger
from src.configs.model_configuration import model_config_document
from src.services.utils.auto_router_utils import (
    PROVIDER_NAME_OVERRIDES,
    get_supported_models_by_provider,
    get_supported_services,
)

client = AsyncNotDiamond(api_key=Config.NOT_DIAMOND_API_KEY) if Config.NOT_DIAMOND_API_KEY else None
logger.info(f"Notdiamond API KEY in SELECT MODEL: {Config.NOT_DIAMOND_API_KEY}")
INTERNAL_TO_NOTDIAMOND_PROVIDER = {value: key for key, value in PROVIDER_NAME_OVERRIDES.items()}

async def apply_auto_model_selection(parsed_data, timer):
    configuration = parsed_data.get("configuration", {})
    execution_time_logs = parsed_data.setdefault("execution_time_logs", [])
    best_model, best_service = await find_best_model(
        service_apikeys=parsed_data.get("service_apikeys") or {},
        prompt=configuration.get("prompt", ""),
        user_message=parsed_data.get("user", ""),
        conversation=configuration.get("conversation", []),
        timer=timer,
        execution_time_logs=execution_time_logs
    )

    if not best_model or not best_service:
        return

    parsed_data["configuration"]["model"] = best_model
    parsed_data["model"] = best_model

    parsed_data["configuration"]["service"] = best_service
    parsed_data["service"] = best_service

    selected_apikey = parsed_data.get("service_apikeys", {}).get(best_service)
    if selected_apikey:
        parsed_data["apikey"] = selected_apikey

async def find_best_model(service_apikeys, prompt, user_message, conversation, timer, execution_time_logs=None):
    available_services = list(service_apikeys.keys())

    conversation_messages = [
        {"role": item["role"], "content": item["content"]}
        for item in conversation
    ]

    supported_services = await get_supported_services()
    supported_models_by_provider = await get_supported_models_by_provider()
    candidate_services = [service_name for service_name in available_services if service_name in supported_services]

    providers = []
    for service_name in candidate_services:
        supported_models = supported_models_by_provider.get(service_name)
        notdiamond_provider = INTERNAL_TO_NOTDIAMOND_PROVIDER.get(service_name, service_name)
        for model, config in model_config_document.get(service_name, {}).items():
            if (
                isinstance(config, dict)
                and config.get("status") == 1
                and config.get("validationConfig", {}).get("type") == "chat"
                and (supported_models and model in supported_models)
            ):
                providers.append({"provider": notdiamond_provider, "model": model})

    if providers and client:
        try: 
            timer.start()
            result = await client.model_router.select_model(
                messages=conversation_messages
                + [
                   {"role": "system", "content": prompt},
                    {"role": "user", "content": user_message},
                ],
                llm_providers=providers,
                tradeoff="cost"
            )
            if execution_time_logs is not None:
                execution_time_logs.append(
                    {"step": "NotDiamond select_model", "time_taken": timer.stop("NotDiamond select_model")}
                )
    
            best_model = result.providers[0].model
            best_service = PROVIDER_NAME_OVERRIDES.get(result.providers[0].provider, result.providers[0].provider)
            return best_model, best_service
    
        except Exception as error:
            logger.error(f"NotDiamond select_model failed: {str(error)}")
            if execution_time_logs is not None:
                execution_time_logs.append(
                    {"step": f"NotDiamond select_model failed: {str(error)}", "time_taken": timer.stop("NotDiamond select_model")}
                )
            return None, None
    
    else:
        return None, None