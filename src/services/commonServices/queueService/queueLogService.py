from config import Config
from src.services.commonServices.queueService.baseQueue import BaseQueue


class Queue2(BaseQueue):
    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance.__init__()
        return cls._instance

    def __init__(self):
        queue_name = Config.LOG_QUEUE_NAME or f"AI-MIDDLEARE-DATA-QUEUE-{Config.ENVIRONMENT}"
        super().__init__(queue_name)
        print("Queue2 Service Initialized")


sub_queue_obj = Queue2()
