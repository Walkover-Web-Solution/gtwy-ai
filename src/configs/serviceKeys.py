from src.configs.service_registry import get_service


def get_service_keys(service):
    """Get service_keys for a service from DB."""
    svc = get_service(service)
    if svc and "service_keys" in svc:
        return svc["service_keys"]
    return {}


async def model_config_change(model_configuration, custom_config, service):
    """Resolve "default" / "max" / "min" placeholders in custom_config.

    "max" and "min" take that bound from the model's configuration. "default" drops
    the key, except anthropic's max_tokens (Anthropic requires it), which gets the
    configured default. Other values are kept. custom_config itself is not modified.
    """
    new_custom_config = custom_config.copy()
    for key, value in custom_config.items():
        if value == "default":
            if service == "anthropic" and key == "max_tokens":
                new_custom_config[key] = model_configuration[key].get("default")
            else:
                del new_custom_config[key]
        elif value in ("max", "min"):
            new_custom_config[key] = model_configuration[key].get(value)
    return new_custom_config


__all__ = ["get_service_keys", "model_config_change"]
