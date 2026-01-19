from fastapi import APIRouter, Depends, Request, HTTPException
import asyncio

from src.services.commonServices.common import chat_multiple_agents, embedding, batch, image
from src.services.commonServices.baseService.utils import make_request_data
from ...middlewares.middleware import jwt_middleware
from ...middlewares.getDataUsingBridgeId import add_configuration_data_to_body
from concurrent.futures import ThreadPoolExecutor
from config import Config
from src.services.commonServices.queueService.queueService import queue_obj
from src.middlewares.ratelimitMiddleware import rate_limit
from globals import *

router = APIRouter()

executor = ThreadPoolExecutor(max_workers= int(Config.max_workers) or 10)

async def auth_and_rate_limit(request: Request):
    await jwt_middleware(request)
    await rate_limit(request,key_path='body.bridge_id' , points=100)
    await rate_limit(request,key_path='body.thread_id', points=20)

@router.post('/chat/completion', dependencies=[Depends(auth_and_rate_limit)])
async def chat_completion(request: Request, db_config: dict = Depends(add_configuration_data_to_body)):
    request.state.is_playground = False
    request.state.version = 2
    data_to_send = await make_request_data(request)
    response_format = data_to_send.get('body',{}).get('configuration', {}).get('response_format', {})
    if response_format and response_format.get('type') != 'default':
        try:
            # Publish the message to the queue
            await queue_obj.publish_message(data_to_send)
            return {"success": True, "message": "Your response will be sent through configured means."}
        except Exception as e:
            # Log the error and return a meaningful error response
            logger.error(f"Failed to publish message: {str(e)}")
            raise HTTPException(status_code=500, detail="Failed to publish message.")
    else:
        # Handle different types of requests
        type = data_to_send.get("body",{}).get('configuration',{}).get('type')
        if type == 'embedding':
            result = await embedding(data_to_send)
            return result
        if type == 'image':
            result = await image(data_to_send)
            return result
        result = await chat_multiple_agents(data_to_send)
        return result


@router.post('/playground/chat/completion/{bridge_id}', dependencies=[Depends(auth_and_rate_limit)])
async def playground_chat_completion_bridge(request: Request, db_config: dict = Depends(add_configuration_data_to_body)):
    request.state.is_playground = True
    request.state.version = 2
    data_to_send = await make_request_data(request)
    org_id = data_to_send['state']['profile']['org']['id']
    bridge_id = data_to_send.get('body',{}).get('bridge_id')
    version_id = data_to_send.get('body',{}).get('version_id')
    channel_id = f"{org_id}_{bridge_id}_{version_id}"
    flag = data_to_send.get('body',{}).get('flag') or False
    if not flag:
        response_format = {
                        "type": "RTLayer",
                        "cred": {
                            "channel": channel_id,
                            "ttl": 1,
                            'apikey': Config.RTLAYER_AUTH
                        }
                    }
        data_to_send['body']['configuration']['response_format'] = response_format
    # Check if response_format is present and publish to queue
    if not flag and response_format and response_format.get('type') != 'default':
        try:
            # Publish the message to the queue
            data_to_send['body']['bridge_configurations']['playground_response_format'] = response_format
            await queue_obj.publish_message(data_to_send)
            return {"success": True, "message": "Your response will be sent through configured means."}
        except Exception as e:
            # Log the error and return a meaningful error response
            logger.error(f"Failed to publish message: {str(e)}")
            raise HTTPException(status_code=500, detail="Failed to publish message.")
    else:
        type = data_to_send.get("body",{}).get('configuration',{}).get('type')
        if type == 'embedding':
                result =  await embedding(data_to_send)
                return result
        result = await chat_multiple_agents(data_to_send)
        return result

@router.post('/batch/chat/completion', dependencies=[Depends(auth_and_rate_limit)])
async def batch_chat_completion(request: Request, db_config: dict = Depends(add_configuration_data_to_body)):
    data_to_send = await make_request_data(request)
    result = await batch(data_to_send)
    return result


@router.post('/testcases', dependencies=[Depends(auth_and_rate_limit)])
async def run_testcases_route(request: Request):
    """
    Execute testcases either from direct input or MongoDB
    
    This route handles testcase execution with support for:
    - Direct testcase data in request body
    - Fetching testcases from MongoDB by bridge_id or testcase_id
    - Parallel processing of multiple testcases
    - Automatic scoring and history saving
    """
    request.state.is_playground = True
    request.state.version = 2
    
    try:
        # Get request body
        body = await request.json()
        org_id = request.state.profile['org']['id']
        
        # Execute testcases using the service
        from src.services.testcase_service import execute_testcases, TestcaseValidationError, TestcaseNotFoundError
        
        result = await execute_testcases(body, org_id)
        return result
        
    except TestcaseValidationError as ve:
        raise HTTPException(status_code=400, detail={"success": False, "error": str(ve)})
    except TestcaseNotFoundError as nfe:
        # Handle not found cases gracefully
        if "No testcase found for the given testcase_id" in str(nfe):
            return {"success": False, "message": str(nfe), "results": []}
        else:
            return {"success": True, "message": str(nfe), "results": []}
    except HTTPException as he:
        raise he
    except Exception as e:
        logger.error(f"Error in run_testcases_route: {str(e)}")
        raise HTTPException(status_code=500, detail={"success": False, "error": f"Internal server error: {str(e)}"})