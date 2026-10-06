from .service_registry import get_service


def get_service_keys(service):
    """Get service_keys for a service from DB."""
    svc = get_service(service)
    if svc and "service_keys" in svc:
        return svc["service_keys"]
    # Return empty dict if not found in DB
    return {}


async def model_config_change(modelConfiguration, custom_config, service):
    new_custom_config = custom_config.copy()
    for key, value in custom_config.items():
        if value == "default":
            if not (service == "anthropic" and key == "max_tokens"):
                del new_custom_config[key]
            else:
                new_custom_config[key] = modelConfiguration[key].get("default")
        elif value == "max":
            max_value = modelConfiguration[key].get("max")
            new_custom_config[key] = max_value

        elif value == "min":
            min_value = modelConfiguration[key].get("min")
            new_custom_config[key] = min_value
    return new_custom_config


# Export the helper function
__all__ = ["get_service_keys", "model_config_change"]
