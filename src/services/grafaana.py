import pyroscope
from config import Config

# Configure Pyroscope Profiling with tags
pyroscope.configure(
    application_name=f"{Config.OTEL_SERVICE_NAME}-{Config.ENVIROMENT}-{'' if Config.CONSUMER_STATUS == 'true' else 'producer'}",
    server_address=Config.PROFILES_SERVER_ADDRESS,
    sample_rate=5,
    detect_subprocesses=False
)
