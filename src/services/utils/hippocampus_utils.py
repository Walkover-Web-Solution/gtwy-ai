import json
from config import Config
from src.services.utils.logger import logger
from src.services.utils.apiservice import fetch

# Hardcoded Hippocampus API URL
HIPPOCAMPUS_API_URL = 'http://hippocampus.gtwy.ai/resource'


async def save_conversation_to_hippocampus(user_message, assistant_message, agent_id, bridge_name=''):
    """
    Save conversation to Hippocampus API for chatbot bridge types.
    
    Args:
        user_message: The user's message content
        assistant_message: The assistant's response content
        agent_id: The bridge/agent ID (used as ownerId)
        bridge_name: The bridge/agent name (used as title)
    """
    try:
        if not Config.HIPPOCAMPUS_API_KEY or not Config.HIPPOCAMPUS_COLLECTION_ID:
            logger.warning("Hippocampus API key or collection ID not configured")
            return
        
        # Create content as stringified JSON with question and answer
        content_obj = {
            "question": user_message,
            "answer": assistant_message
        }
        content = json.dumps(content_obj)
        
        # Use bridge_name if available, otherwise fallback to agent_id
        title = bridge_name if bridge_name else agent_id
        
        payload = {
            "collectionId": Config.HIPPOCAMPUS_COLLECTION_ID,
            "title": title,
            "ownerId": agent_id,
            "content": content,



            "settings": {
                "strategy": "custom",
                "chunkingUrl": "https://flow.sokt.io/func/scriQywSNndU",
                "chunkSize": 4000
            }
        }
        
        headers = {
            "x-api-key": Config.HIPPOCAMPUS_API_KEY,
            "Content-Type": "application/json"
        }
        

        response_data, response_headers = await fetch(
            url=HIPPOCAMPUS_API_URL,
            method="POST",
            headers=headers,
            json_body=payload
        )
        
        logger.info(f"Successfully saved conversation to Hippocampus for agent_id: {agent_id}")
                
    except Exception as e:
        logger.error(f"Error saving conversation to Hippocampus: {str(e)}")

