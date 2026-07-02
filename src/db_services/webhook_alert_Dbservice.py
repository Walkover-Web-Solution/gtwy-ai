from globals import logger
from models.mongo_connection import db
from src.services.utils.time import with_timeout

alertModel = db["alerts"]


async def get_webhook_data(org_id):
    try:
        webhook_data = await with_timeout(alertModel.find({"org_id": org_id}).to_list(length=None))
        return {"webhook_data": webhook_data or []}
    except Exception as error:
        logger.error(f"Error in get_webhook_data: %s, {str(error)}")
        return {"success": False, "error": error}
