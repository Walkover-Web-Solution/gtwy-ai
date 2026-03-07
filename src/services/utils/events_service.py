from config import Config
from src.services.utils.apiservice import fetch
from src.services.utils.logger import logger


async def send_api_hit_event(message_id, org_id):
    try:
        url = Config.EVENTS_API_URL
        api_key = Config.EVENTS_API_KEY
        code = Config.EVENTS_API_CODE
        if not url or not api_key or not code:
            return
        await fetch(
            url=url,
            method="POST",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json_body={
                "event": {
                    "transaction_id": message_id,
                    "external_subscription_id": str(org_id),
                    "code": code,
                }
            },
        )
    except Exception as e:
        logger.error(f"Failed to send api hit event: {e}")
