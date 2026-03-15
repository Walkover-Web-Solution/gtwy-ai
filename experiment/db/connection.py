from db.json_store import init_store


async def init_db():
    """Initialize JSON file store."""
    await init_store()


async def close_db():
    """No-op: JSON store needs no teardown."""
    print("JSON store closed (no-op).")


def get_db():
    """Unused — kept for import compatibility."""
    return None
