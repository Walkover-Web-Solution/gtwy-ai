import os

# Importing src pulls in config-driven clients (Redis, Mongo, billing). They connect
# lazily, so harmless local defaults are enough for unit tests; nothing here talks
# to a real service.
os.environ.setdefault("REDIS_URI", "redis://localhost:6379/0")
os.environ.setdefault("MONGODB_CONNECTION_URI", "mongodb://localhost:27017")
os.environ.setdefault("MONGODB_DATABASE_NAME", "test")
os.environ.setdefault("max_workers", "2")
os.environ.setdefault("ENVIRONMENT", "test")
os.environ.setdefault("LAGO_CREDIT_RATE_USD", "0.001")
os.environ.setdefault("GTWY_COMMISSION_PCT", "10")
