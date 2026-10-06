from globals import logger
from models.mongo_connection import db
from src.services.utils.time import with_timeout

modelConfigModel = db["modelconfigurations"]
platformApikeyModel = db["platform_apikeys"]


async def get_platform_apikeys():
    """Load the platform's own provider keys (wallet-billed traffic).

    Stored encrypted in the platform_apikeys collection (written by Node's
    admin API with Helper.encrypt — the same method customer apikeys use) and
    decrypted here once at load with the very same Helper.decrypt; the gate then
    does plain dict lookups.
    """
    # Lazy import, and it MUST stay lazy. helper.py imports
    # src.configs.model_configuration, which imports this module — so importing
    # Helper at the top of this file closes that loop and breaks startup. By the
    # time this function is CALLED (from the lifespan, after every module is
    # loaded) the cycle is long since resolved, so the import is safe here.
    from src.services.utils.helper import Helper

    keys = {}
    try:
        docs = await with_timeout(platformApikeyModel.find({}, {"_id": 0, "service": 1, "apikey": 1}).to_list(length=None))
        for doc in docs:
            service, encrypted = doc.get("service"), doc.get("apikey")
            if not (service and encrypted):
                continue
            try:
                keys[service] = Helper.decrypt(encrypted)
            except Exception as e:
                logger.error(f"Could not decrypt platform apikey for service '{service}': {e}")
    except Exception as error:
        logger.error(f"Error fetching platform apikeys: {error}")
    return keys


async def get_model_configurations():
    try:
        # Remove the projection to allow _id to be included in the results
        configurations = await with_timeout(modelConfigModel.find({"status": 1}, {"_id": 0}).to_list(length=None))
        config_dict = {}
        for conf in configurations:
            conf_dict = dict(conf)
            if "outputConfig" in conf_dict:
                if "_id" in conf_dict["outputConfig"]["usage"][0]:
                    del conf_dict["outputConfig"]["usage"][0]["_id"]
            if config_dict.get(conf["service"]) is None:
                config_dict[conf["service"]] = {}
            config_dict[conf["service"]][conf["model_name"]] = conf

        return config_dict
    except Exception as error:
        logger.error(f"Error fetching model configurations:, {error}")
        return {}
