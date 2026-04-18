from globals import logger
from config import Config

try:
    import pyroscope
    # Configure Pyroscope Profiling with tags
    pyroscope.configure(
        application_name=Config.OTEL_SERVICE_NAME,
        server_address=Config.PROFILES_SERVER_ADDRESS,
        sample_rate=5,
        tags={
        "env": Config.ENVIRONMENT,
        "service_name": Config.OTEL_SERVICE_NAME,
        "service_type": 'consumer' if Config.CONSUMER_STATUS == 'true' else 'producer'
    }
    )
except ImportError:
    logger.warning("Pyroscope not installed, skipping profiling configuration")
except Exception as e:
    logger.error("Pyroscope failed to initialize: %s", e)
