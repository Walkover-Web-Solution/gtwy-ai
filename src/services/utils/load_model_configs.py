from globals import logger
from models.mongo_connection import db

modelConfigModel = db["modelconfigurations"]


async def get_model_configurations():
    try:
        # Remove the projection to allow _id to be included in the results
        configurations = await modelConfigModel.find({}, {"_id": 0}).to_list(length=None)
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
