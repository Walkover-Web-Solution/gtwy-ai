from globals import logger
from models.mongo_connection import db
from src.services.utils.time import with_timeout

serviceConfigModel = db["services"]


async def get_service_configs():
    """Load the service registry from the `services` collection.

    Returns a dict keyed by service_name. Returns an empty dict on any error so
    callers can fall back to the hardcoded defaults in service_registry.py.
    """
    try:
        services = await with_timeout(serviceConfigModel.find({"status": 1}, {"_id": 0}).to_list(length=None))
        config_dict = {}
        for svc in services:
            svc_dict = dict(svc)
            name = svc_dict.get("service_name")
            if not name:
                continue
            config_dict[name] = svc_dict
        return config_dict
    except Exception as error:
        logger.error(f"Error fetching service configs:, {error}")
        return {}
