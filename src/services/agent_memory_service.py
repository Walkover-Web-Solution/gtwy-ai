"""
Agent Memory Service with Canonicalizer Integration

Stores relevant conversations in Hippocampus and tracks frequency in MongoDB.
Uses call_ai_middleware with the canonicalizer bridge for question processing.
"""

import json
import logging
from typing import Optional, Tuple
from config import Config
from src.services.utils.apiservice import fetch

logger = logging.getLogger(__name__)

HIPPOCAMPUS_SEARCH_URL = 'http://hippocampus.gtwy.ai/search'
HIPPOCAMPUS_RESOURCE_URL = 'http://hippocampus.gtwy.ai/resource'


async def call_canonicalizer_agent(
    system_prompt: str,
    user_message: str,
    llm_response: str,
) -> Optional[dict]:

    try:
        from src.configs.constant import bridge_ids
        from src.services.utils.ai_call_util import call_ai_middleware

        user = f"System: {system_prompt}\n\nUser: {user_message}\n\nAssistant: {llm_response}"

        # call_ai_middleware handles auth and returns the parsed JSON dict directly
        result = await call_ai_middleware(
            user=user,
            bridge_id=bridge_ids["canonicalizer"]
        )
        return result

    except Exception as e:
        logger.error(f"Agent Memory Service: Error calling canonicalizer agent: {str(e)}")
        return None


async def search_hippocampus_for_memories(
    canonical_question: str,
    agent_id: str,
    top_k: int = 5,
    limit: int = 5,
    minScore: float = 0.9
) -> Tuple[Optional[str], float]:

    try:
        headers = {
            'x-api-key': Config.HIPPOCAMPUS_API_KEY,
            'Content-Type': 'application/json'
        }
        
        payload = {
            'query': canonical_question,
            'ownerId': agent_id,
            'collectionId': Config.HIPPOCAMPUS_COLLECTION_ID,
            'top_k': top_k,
            'limit': limit,
            'minScore': minScore
        }
        
        response_data, _ = await fetch(
            url=HIPPOCAMPUS_SEARCH_URL,
            method="POST",
            headers=headers,
            json_body=payload
        )
        
        if response_data and 'result' in response_data:
            results = response_data['result']
            if results and len(results) > 0:
                top_result = results[0]
                resource_id = top_result.get('payload', {}).get('resourceId')
                score = top_result.get('score', 0.0)
                logger.info(f"Agent Memory Service: Top memory match: resource_id={resource_id}, score={score:.1%}")
                return resource_id, score
        
        return None, 0.0
        
    except Exception as e:
        logger.error(f"Agent Memory Service: Error searching Hippocampus: {str(e)}")
        return None, 0.0


async def update_frequency_in_mongodb(resource_id: str) -> bool:

    try:
        from src.models.agent_memory_model import increment_memory_frequency
        success = await increment_memory_frequency(resource_id)
        
        if success:
            logger.info(f"Agent Memory Service: Incremented frequency for resource_id: {resource_id}")
        else:
            logger.warning(f"Agent Memory Service: Memory record not found for resource_id: {resource_id}")
        
        return success
        
    except Exception as e:
        logger.error(f"Agent Memory Service: Error updating frequency: {str(e)}")
        return False


async def create_memory_in_hippocampus_and_mongodb(
    canonical_question: str,
    original_answer: Optional[str],
    agent_id: str,
    bridge_name: str = ""
) -> bool:

    try:
        # Create in Hippocampus
        content = json.dumps({
            "question": canonical_question,
            "answer": original_answer or ""  # Empty string if no answer
        })
        
        payload = {
            "collectionId": Config.HIPPOCAMPUS_COLLECTION_ID,
            "title": bridge_name if bridge_name else agent_id,
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
        
        response_data, _ = await fetch(
            url=HIPPOCAMPUS_RESOURCE_URL,
            method="POST",
            headers=headers,
            json_body=payload
        )
        
        resource_id = response_data.get('_id') if response_data else None
        
        if not resource_id:
            logger.error("Agent Memory Service: Failed to create resource in Hippocampus")
            return False
        
        # Create in MongoDB (only store answer if provided)
        from src.models.agent_memory_model import create_memory_record
        await create_memory_record(
            resource_id=resource_id,
            agent_id=agent_id,
            canonical_question=canonical_question,
            original_answer=original_answer  # Will be None if save_response=false
        )
        
        answer_status = "with answer" if original_answer else "question only"
        logger.info(f"Agent Memory Service: Created new memory ({answer_status}, frequency=1) for agent_id: {agent_id}")
        return True
        
    except Exception as e:
        logger.error(f"Agent Memory Service: Error creating memory: {str(e)}")
        return False


async def save_to_agent_memory(
    user_question: str,
    assistant_answer: str,
    agent_id: str,
    system_prompt: str,
    bridge_name: str = ""
) -> bool:

    try:
        if not Config.HIPPOCAMPUS_API_KEY or not Config.HIPPOCAMPUS_COLLECTION_ID:
            logger.warning("Agent Memory Service: Hippocampus not configured")
            return False

        # Step 1: Search Hippocampus FIRST with original user question
        logger.info(f"Agent Memory Service: Searching Hippocampus for similar question: '{user_question[:50]}...'")
        resource_id, score = await search_hippocampus_for_memories(
            canonical_question=user_question,
            agent_id=agent_id,
            top_k=5,
            limit=5,
            minScore=0.9  # 90% threshold
        )

        # Step 2: If match found, just increment frequency (skip Canonicalizer)
        if resource_id:
            logger.info(f"Agent Memory Service: Match found (score={score:.1%}), incrementing frequency - skipping Canonicalizer")
            return await update_frequency_in_mongodb(resource_id)

        # Step 3: No match found - Call Canonicalizer
        logger.info("Agent Memory Service: No match found, calling Canonicalizer to process question")
        canonical_data = await call_canonicalizer_agent(
            system_prompt=system_prompt,
            user_message=user_question,
            llm_response=assistant_answer,
        )
        
        if not canonical_data:
            logger.error("Agent Memory Service: Failed to get response from Canonicalizer")
            return False
        
        # Step 4: Check if agent-level
        if not canonical_data.get('is_agent_level', False):
            logger.info(f"Agent Memory Service: Not agent-level - not saving: '{user_question[:50]}...'")
            return False
        
        canonical_question = canonical_data.get('question')
        if not canonical_question:
            logger.error("Agent Memory Service: Canonicalizer did not return canonical question")
            return False
        
        # Determine if we should save the response
        save_response = canonical_data.get('save_response', False)
        
        # Step 5: Create new memory with canonical question
        logger.info(f"Agent Memory Service: Creating new memory for canonical question: '{canonical_question}'")
        return await create_memory_in_hippocampus_and_mongodb(
            canonical_question=canonical_question,
            original_answer=assistant_answer if save_response else None,
            agent_id=agent_id,
            bridge_name=bridge_name
        )
        
    except Exception as e:
        logger.error(f"Agent Memory Service: Error in save_to_agent_memory: {str(e)}")
        return False

