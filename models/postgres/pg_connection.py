import importlib.util
import os
import sys

import sqlalchemy as sa
from sqlalchemy.orm import declarative_base, sessionmaker

from config import Config
from globals import logger

Base = declarative_base()
db = {}

DB_NAME = Config.DB_NAME
DB_USER = Config.DB_USER
DB_PASS = Config.DB_PASS
DB_HOST = Config.DB_HOST

DATABASE_URL = f"postgresql://{DB_USER}:{DB_PASS}@{DB_HOST}/{DB_NAME}"
engine = sa.create_engine(DATABASE_URL, pool_pre_ping=True)
Session = sessionmaker(bind=engine, autoflush=False)

retry_strategy = {
    "max_retries": 100,
    "pool_recycle": 300,
}


# Function to sync the database
def init_dbservice():
    try:
        Base.metadata.create_all(engine)
        print("Connected to postgres")
    except Exception as error:
        logger.error(f"Unable to connect to the database: {str(error)}")


init_dbservice()


def load_models():
    current_dir = os.path.dirname(os.path.realpath(__file__))
    files = [
        f
        for f in os.listdir(current_dir)
        if os.path.isfile(os.path.join(current_dir, f)) and f.endswith(".py") and f != os.path.basename(__file__)
    ]

    for file in files:
        spec = importlib.util.spec_from_file_location(file[:-3], os.path.join(current_dir, file))
        module = importlib.util.module_from_spec(spec)
        sys.modules[file[:-3]] = module
        spec.loader.exec_module(module)
        if hasattr(module, "default"):
            model = module.default(engine, sa)
            db[model.__name__] = model


load_models()

for model_name in db:
    model = db[model_name]
    if hasattr(model, "associate"):
        model.associate(db)

db["engine"] = engine
db["session"] = Session

metadata = sa.MetaData()

# Reflect the table from the database
metadata.reflect(bind=engine)


# This dictionary is now ready to be used.
