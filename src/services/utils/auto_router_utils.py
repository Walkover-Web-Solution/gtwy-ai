import asyncio
import traceback

import httpx

from config import Config
from globals import logger
from src.configs.model_configuration import model_config_document

SUPPORTED_SERVICES_STORE = {"services": set(), "models_by_provider": {}}
SUPPORTED_SERVICES_REFRESH_INTERVAL_SECONDS = 24 * 60 * 60
PROVIDER_NAME_OVERRIDES = {
    "google": "gemini",
}


async def refresh_supported_services():
    """Refresh supported provider list from NotDiamond into in-memory store."""
    try:
        async with httpx.AsyncClient(timeout=15.0) as http_client:
            response = await http_client.get(
                "https://api.notdiamond.ai/v2/models",
                headers={
                    "Authorization": f"Bearer {Config.NOT_DIAMOND_API_KEY}",
                    "Content-Type": "application/json",
                },
            )
        response.raise_for_status()

        models = response.json().get("models", [])

        services = set()
        models_by_provider = {}
        for model in models:
            if isinstance(model, dict):
                provider = model.get("provider")
                model_name = model.get("model") or model.get("name")
            else:
                provider = getattr(model, "provider", None)
                model_name = getattr(model, "model", None) or getattr(model, "name", None)
            if provider:
                normalized_provider = PROVIDER_NAME_OVERRIDES.get(provider, provider)
                services.add(normalized_provider)
                if model_name:
                    models_by_provider.setdefault(normalized_provider, set()).add(model_name)

        if services:
            SUPPORTED_SERVICES_STORE["services"] = services
        if models_by_provider:
            SUPPORTED_SERVICES_STORE["models_by_provider"] = models_by_provider
    except Exception as error:
        logger.error(f"Error refreshing NotDiamond supported services: {str(error)}, {traceback.format_exc()}")


async def run_supported_services_refresh_loop():
    """Background loop that refreshes NotDiamond supported providers every 15 days."""
    await refresh_supported_services()
    while True:
        await asyncio.sleep(SUPPORTED_SERVICES_REFRESH_INTERVAL_SECONDS)
        await refresh_supported_services()


async def get_supported_services():
    services = SUPPORTED_SERVICES_STORE.get("services") or set()
    return services or set(model_config_document.keys())


async def get_supported_models_by_provider():
    return SUPPORTED_SERVICES_STORE.get("models_by_provider") or {}
