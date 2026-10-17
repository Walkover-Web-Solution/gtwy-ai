from config import Config
from src.services.commonServices.queueService.baseQueue import BaseQueue


class NotificationQueue(BaseQueue):
    """Producer for the notification hub's queue, consumed by the Node backend."""

    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self):
        super().__init__(Config.NOTIFICATION_QUEUE_NAME)


notification_queue_obj = NotificationQueue() if Config.NOTIFICATION_QUEUE_NAME else None
