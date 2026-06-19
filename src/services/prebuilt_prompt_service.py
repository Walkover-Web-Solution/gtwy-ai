from fastapi import HTTPException

from models.mongo_connection import db
from src.services.utils.time import with_timeout

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
        document = await with_timeout(prebuilt_db.find_one(query, projection))

        if document and document.get("prebuilt_prompts") and document["prebuilt_prompts"].get(prompt_key):
            return {prompt_key: document["prebuilt_prompts"][prompt_key]}

        return None

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Database error: {str(e)}") from e


async def get_multiple_prebuilt_prompts_without_org_service(prompt_keys: list[str]) -> dict:
    try:
        query = {"$or": [{key: {"$exists": True}} for key in prompt_keys]}
        projection = {"_id": 0, **{key: 1 for key in prompt_keys}}
        result = {}
        documents = await with_timeout(prebuilt_db.find(query, projection).sort("_id", -1).to_list(length=None))
        for document in documents:
            for key in prompt_keys:
                if key not in result and document.get(key):
                    result[key] = document[key]
            if all(key in result for key in prompt_keys):
                break
        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Database error: {str(e)}") from e


async def get_specific_prebuilt_prompt_without_org_service(prompt_key: str):
    try:
        query = {
            "$or": [
                {prompt_key: {"$exists": True}},
            ]
        }
        projection = {"_id": 0, prompt_key: 1}
        document = await with_timeout(prebuilt_db.find_one(query, projection, sort=[("_id", -1)]))

        if document and document.get(prompt_key):
            return {prompt_key: document[prompt_key]}

        return None

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Database error: {str(e)}") from e
