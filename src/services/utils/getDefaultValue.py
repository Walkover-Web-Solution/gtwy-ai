from fastapi import HTTPException

from src.configs.model_configuration import model_config_document

from ...configs.constant import service_name


def validate_fall_back(fall_back_data):
    """
    Validate fall_back configuration structure and service/model availability.

    Args:
        fall_back_data: Dictionary containing fall_back configuration

    Returns:
        bool: True if valid, False otherwise

    Raises:
        HTTPException: If validation fails with specific error message
    """
    if not isinstance(fall_back_data, dict):
        raise HTTPException(status_code=400, detail="fall_back must be a dictionary")

    # Check required fields
    required_fields = ["is_enable", "service", "model"]
    for field in required_fields:
        if field not in fall_back_data:
            raise HTTPException(status_code=400, detail=f"fall_back missing required field: {field}")

    # Validate is_enable is boolean
    if not isinstance(fall_back_data["is_enable"], bool):
        raise HTTPException(status_code=400, detail="fall_back.is_enable must be a boolean")

    # If fall_back is enabled, validate service and model
    if fall_back_data["is_enable"]:
        service = fall_back_data["service"]
        model = fall_back_data["model"]

        # Check if service exists in model configuration
        if service not in model_config_document:
            raise HTTPException(status_code=400, detail=f"fall_back service '{service}' is not available")

        # Check if model exists for the service
        if model not in model_config_document[service]:
            raise HTTPException(
                status_code=400, detail=f"fall_back model '{model}' is not available for service '{service}'"
            )

    return True


async def get_default_values_controller(service, model, current_configuration, type):
    try:
        service = service.lower()

        def get_default_values(config):
            default_values = {}
            config_items = config.get("configuration", {})

            for key, value in config_items.items():
                current_value = current_configuration.get(key)

                if current_value == "min":
                    default_values[key] = "min"
                elif current_value == "max":
                    default_values[key] = "max"
                elif current_value == "default":
                    if type == "embedding":
                        default_values[key] = config_items[key]["default"]
                    else:
                        default_values[key] = "default"
                else:
                    if key in config_items:
                        if key == "model":
                            default_values[key] = value.get("default", None)
                            continue
                        if key == "response_type":
                            current_type = current_value.get("type") if isinstance(current_value, dict) else None
                            if current_type and any(
                                opt.get("type") == current_type for opt in config_items[key]["options"]
                            ):
                                default_values[key] = current_value
                                if current_type == "json_schema":
                                    default_values["response_type"]["json_schema"] = current_value.get(
                                        "json_schema", None
                                    )
                            else:
                                if isinstance(value.get("default"), dict):
                                    json_key = value.get("default").get("key")
                                    default_values[key] = {json_key: value.get("default", None).get(json_key)}
                                else:
                                    default_values[key] = value.get("default", None)
                            continue
                        min_value = value.get("min")
                        max_value = value.get("max")
                        if min_value is not None and max_value is not None:
                            if current_value is not None and not (min_value <= current_value <= max_value):
                                default_values[key] = value.get("default", None)
                            else:
                                if current_value is None:
                                    default_values[key] = "default"
                                else:
                                    default_values[key] = current_value
                        else:
                            if current_value is None:
                                default_values[key] = "default"
                            else:
                                default_values[key] = current_value
                    else:
                        default_values[key] = (
                            value.get("default", None) if key == "model" or type == "embedding" else "default"
                        )

            return default_values

        modelObj = model_config_document[service][model]

        if modelObj is None:
            raise HTTPException(status_code=400, detail=f"Invalid model: {model}")

        if service == service_name["openai"]:
            return get_default_values(modelObj)
        elif service == service_name["anthropic"]:
            return get_default_values(modelObj)
        elif service == service_name["groq"]:
            return get_default_values(modelObj)
        elif service == service_name["grok"]:
            return get_default_values(modelObj)
        elif service == service_name["open_router"]:
            return get_default_values(modelObj)
        elif service == service_name["mistral"]:
            return get_default_values(modelObj)
        elif service == service_name["gemini"]:
            return get_default_values(modelObj)
        elif service == service_name["ai_ml"]:
            return get_default_values(modelObj)
        elif service == service_name["openai_completion"]:
            return get_default_values(modelObj)

        else:
            raise HTTPException(status_code=404, detail=f"Service '{service}' not found.")

    except Exception as e:
        raise HTTPException(status_code=401, detail=str(e)) from e
