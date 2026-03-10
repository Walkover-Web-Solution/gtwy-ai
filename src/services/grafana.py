import pyroscope
from globals import logger
from config import Config

try:
    # Configure Pyroscope Profiling with tags
    pyroscope.configure(
        application_name=f"{Config.OTEL_SERVICE_NAME}-{Config.ENVIROMENT}-{'' if Config.CONSUMER_STATUS == 'true' else 'producer'}",
        server_address=Config.PROFILES_SERVER_ADDRESS,
        sample_rate=5
    )
except Exception as e:
    logger.error("Pyroscope failed to initialize: %s", e)
