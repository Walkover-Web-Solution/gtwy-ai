from fastapi import HTTPException

from models.mongo_connection import db

prebuilt_db = db["preBuiltPrompts"]


async def get_specific_prebuilt_prompt_service(org_id: str, prompt_key: str):
    """
    Retrieve a specific prebuilt prompt for an organization

    Args:
        org_id (str): Organization ID
        prompt_key (str): The key of the prompt to retrieve

    Returns:
        dict: Dictionary containing the prompt key and value, or None if not found
    """
    try:
        # Query specific prebuilt prompt for the organization
        query = {"org_id": org_id}
        projection = {"_id": 0, f"prebuilt_prompts.{prompt_key}": 1}
        document = await prebuilt_db.find_one(query, projection)

        if document and document.get("prebuilt_prompts") and document["prebuilt_prompts"].get(prompt_key):
            return {prompt_key: document["prebuilt_prompts"][prompt_key]}

        return None

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Database error: {str(e)}") from e
