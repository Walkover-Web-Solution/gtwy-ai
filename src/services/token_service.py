from src.services.cache_service import find_in_cache
from globals import logger

async def is_token_blacklisted(token: str) -> bool:
    """
    Check if a JWT token is blacklisted
    
    Args:
        token: The JWT token to check
        
    Returns:
        bool: True if token is blacklisted, False otherwise
    """
    if not token:
        return False
    
    try:
        # Check if token exists in blacklist cache
        # Node.js stores it as "blacklist:{token}"
        result = await find_in_cache(f"blacklist:{token}")
        return result is not None
    except Exception as e:
        logger.error(f"Error checking token blacklist: {str(e)}")
        return False