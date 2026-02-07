import sqlalchemy.ext.asyncio as sa_async
from sqlalchemy import text
from sqlalchemy.ext.declarative import declarative_base

from config import Config
from globals import logger, traceback

# Configuration
DATABASE_URL = Config.TIMESCALE_SERVICE_URL.replace("postgresql://", "postgresql+asyncpg://").replace(
    "?sslmode=require", ""
)

# Async Engine and session setup
async_engine = sa_async.create_async_engine(DATABASE_URL, pool_pre_ping=True)
AsyncSession = sa_async.async_sessionmaker(bind=async_engine, autoflush=False)

Base = declarative_base()


# Function to test async database connection
async def init_async_dbservice():
    try:
        async with async_engine.begin() as conn:
            await conn.execute(text("SELECT 1"))
        print("Connected to the Timescale database.")
    except Exception as error:
        logger.error(f"Unable to connect to the database: {str(error)} {traceback.format_exc()}")


# Initialize database connection
db = {
    "engine": async_engine,
    "session": AsyncSession,
}
