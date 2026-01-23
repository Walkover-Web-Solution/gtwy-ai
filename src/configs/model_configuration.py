import asyncio

from pymongo.errors import OperationFailure, PyMongoError

from config import Config
from globals import logger
from models.mongo_connection import db
from src.services.commonServices.baseService.utils import send_message
from src.services.utils.load_model_configs import get_model_configurations

model_config_model = db["modelconfigurations"]
model_config_document = {}


async def init_model_configuration():
    """Initializes or refreshes the model configuration document."""
    global model_config_document
    try:
        new_document = await get_model_configurations()
        model_config_document.clear()  # Clear old config before updating
        model_config_document.update(new_document)
        logger.info("Model configurations refreshed successfully.")
    except Exception as e:
        logger.error(f"Error refreshing model configurations: {e}")


async def _async_change_listener():
    """The core async change stream listener."""
    pipeline = [{"$match": {"operationType": {"$in": ["insert", "update", "replace", "delete"]}}}]
    try:
        async with model_config_model.watch(pipeline, full_document="updateLookup") as stream:
            logger.info("MongoDB change stream is now listening for model configuration changes.")
            async for change in stream:
                logger.info(f"Change detected in model configurations: {change['operationType']}")
                await init_model_configuration()
                await send_message(
                    cred={"apikey": Config.RTLAYER_AUTH, "ttl": 1, "channel": "global_model_updates"},
                    data={
                        "event": "model_config_updated",
                        "operation": change["operationType"],
                        "model_name": change.get("fullDocument", {}).get("model_name"),
                        "service": change.get("fullDocument", {}).get("service"),
                        "timestamp": str(change.get("clusterTime", "")),
                    },
                )
                logger.info("Model configuration change detected and sent to RTLayer successfully.")
    except OperationFailure as e:
        logger.error(f"Change stream operation failed: {e}")
        raise  # Re-raise to be caught by the sync wrapper
    except Exception as e:
        logger.error(f"An unexpected error occurred in the async listener: {e}")
        raise


async def background_listen_for_changes():
    """An asynchronous change stream listener with a retry loop, designed to run as a background task."""
    while True:
        try:
            await _async_change_listener()
        except (OperationFailure, PyMongoError) as e:
            logger.error(f"MongoDB connection error in change stream: {e}. Reconnecting in 5 seconds...")
            await asyncio.sleep(5)
        except Exception as e:
            logger.error(
                f"An unexpected error occurred in background_listen_for_changes: {e}. Restarting in 10 seconds..."
            )
            await asyncio.sleep(10)
