from config import Config
from src.services.commonServices.queueService.baseQueue import BaseQueue


class Queue2(BaseQueue):
    """
    Queue service for log data processing.
    
    NOTE: Events API tracking (api_hit events for billing/usage) is handled
    by Node's logQueueConsumer, NOT by Python. The validateResponse payload
    already contains message_id and org_id which Node uses to call the events API.
    Do NOT add direct events API calls here - see sendApiHitEvent.service.js in gtwy.
    """
    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance.__init__()
        return cls._instance

    def __init__(self):
        queue_name = Config.LOG_QUEUE_NAME or f"AI-MIDDLEARE-DATA-QUEUE-{Config.ENVIROMENT}"
        super().__init__(queue_name)
        print("Queue2 Service Initialized")


sub_queue_obj = Queue2()
