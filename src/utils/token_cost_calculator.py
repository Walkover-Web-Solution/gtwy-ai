from typing import Optional

from globals import logger
from src.configs.model_configuration import model_config_document


def estimate_cost(
    service: str, model: str, max_tokens: int, input_tokens: Optional[int] = None
) -> float:
    """
    Estimate the worst-case cost for a request.
    Uses max_tokens as output estimate and input_tokens if provided.
    Fetches pricing from model_config_document.

    Args:
        service: 'openai', 'anthropic', 'google', etc.
        model: Model name
        max_tokens: Maximum output tokens (worst case)
        input_tokens: Actual input tokens (if known)

    Returns:
        Estimated cost in USD
    """
    try:
        service_lower = service.lower()
        model_lower = model.lower()

        service_config = model_config_document.get(service_lower, {})
        model_config = service_config.get(model_lower)

        if not model_config:
            logger.warning(
                f"No pricing found for {service}/{model}, using default $0.01"
            )
            return 0.01

        output_config = model_config.get("outputConfig", {})
        usage_config = output_config.get("usage", [{}])[0]

        input_price = float(usage_config.get("inputPrice", 0))
        output_price = float(usage_config.get("outputPrice", 0))

        input_cost = (input_tokens or 0) * input_price / 1000
        output_cost = max_tokens * output_price / 1000

        total_cost = input_cost + output_cost

        return round(total_cost, 6)

    except Exception as e:
        logger.error(f"Error estimating cost: {str(e)}")
        return 0.01


def calculate_actual_cost(
    service: str, model: str, tokens_in: int, tokens_out: int
) -> float:
    """
    Calculate actual cost based on real token usage.
    Fetches pricing from model_config_document.

    Args:
        service: 'openai', 'anthropic', 'google', etc.
        model: Model name
        tokens_in: Actual input tokens
        tokens_out: Actual output tokens

    Returns:
        Actual cost in USD
    """
    try:
        service_lower = service.lower()
        model_lower = model.lower()

        service_config = model_config_document.get(service_lower, {})
        model_config = service_config.get(model_lower)

        if not model_config:
            logger.warning(
                f"No pricing found for {service}/{model}, using default $0.01"
            )
            return 0.01

        output_config = model_config.get("outputConfig", {})
        usage_config = output_config.get("usage", [{}])[0]

        input_price = float(usage_config.get("inputPrice", 0))
        output_price = float(usage_config.get("outputPrice", 0))

        input_cost = tokens_in * input_price / 1000
        output_cost = tokens_out * output_price / 1000

        total_cost = input_cost + output_cost

        return round(total_cost, 6)

    except Exception as e:
        logger.error(f"Error calculating actual cost: {str(e)}")
        return 0.01
