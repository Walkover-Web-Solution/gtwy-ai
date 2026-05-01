from config import Config
from src.services.commonServices.queueService.baseQueue import BaseQueue


class Queue3(BaseQueue):
    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self):
        queue_name = Config.METRICS_QUEUE_NAME or f"AI-MIDDLEWARE-METRICS-QUEUE-{Config.ENVIRONMENT}"
        super().__init__(queue_name)
        print("Queue3 Metrics Service Initialized")


metrics_queue_obj = Queue3()
