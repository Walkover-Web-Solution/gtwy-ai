from globals import logger
from models.mongo_connection import db

alertModel = db["alerts"]


async def get_webhook_data(org_id):
    try:
        webhook_data = await alertModel.find({"org_id": org_id}).to_list(length=None)
        return {"webhook_data": webhook_data or []}
    except Exception as error:
        logger.error(f"Error in get_webhook_data: {str(error)}")
        return {"webhook_data": []}
