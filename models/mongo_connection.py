import certifi
from motor.motor_asyncio import AsyncIOMotorClient
from pymongo import errors

from config import Config
from globals import logger

try:
    client = AsyncIOMotorClient(
        Config.MONGODB_CONNECTION_URI,
        tls=True,  # Use 'tls' instead of 'ssl'
        tlsCAFile=certifi.where(),  # Use 'tlsCAFile' instead of 'ssl_ca_certs'
    )
    db = client[Config.MONGODB_DATABASE_NAME]
    print("connected to Mongo...")
except errors.ConnectionFailure as e:  # Use 'ConnectionFailure' instead of 'ConnectionError'
    logger.error(f"Could not connect to MongoDB: {str(e)}")
