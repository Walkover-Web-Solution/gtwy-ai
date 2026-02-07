from .postgres.pg_connection import db
from .Timescale.connections import db as timescale_db

combined_models = {"pg": db, "timescale": timescale_db}

# Exporting the combined models dictionary
# This is typically not needed in Python, but if you want to make it accessible as a module variable, you can use:
