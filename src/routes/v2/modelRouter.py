import asyncio
from concurrent.futures import ThreadPoolExecutor
import uuid
from fastapi import APIRouter, Depends, HTTPException, Request, WebSocket, WebSocketDisconnect

from config import Config
from globals import logger
from src.middlewares.ratelimitMiddleware import rate_limit
from src.services.commonServices.baseService.utils import make_request_data
from src.services.commonServices.common import batch, chat_multiple_agents, embedding, image
from src.services.commonServices.queueService.queueService import queue_obj

from ...middlewares.getDataUsingBridgeId import add_configuration_data_to_body
from ...middlewares.middleware import jwt_middleware
from src.middlewares.openai_sdk_middleware import openai_sdk_middleware
from src.services.utils.openai_sdk_utils import run_openai_chat_and_format

router = APIRouter()

executor = ThreadPoolExecutor(max_workers=int(Config.max_workers) or 10)


async def auth_and_rate_limit(request: Request):
    await jwt_middleware(request)
    await rate_limit(request, key_path="body.bridge_id", points=100)
    await rate_limit(request, key_path="body.thread_id", points=20)


@router.post("/chat/completion", dependencies=[Depends(auth_and_rate_limit)])
async def chat_completion(request: Request, db_config: dict = Depends(add_configuration_data_to_body)):
    request.state.is_playground = False
    request.state.version = 2
    data_to_send = await make_request_data(request)

    message_id = str(uuid.uuid1())
    data_to_send["body"]["message_id"] = message_id
    
    response_format = data_to_send.get("body", {}).get("configuration", {}).get("response_format", {})
    mode = data_to_send.get("body", {}).get("mode")
    if (response_format and response_format.get("type") != "default") or mode == "todo":
        try:
            # Publish the message to the queue
            await queue_obj.publish_message(data_to_send)
            return {"success": True, "message_id": message_id, "message": "Your response will be sent through configured means."}
        except Exception as e:
            # Log the error and return a meaningful error response
            logger.error(f"Failed to publish message: {str(e)}")
            raise HTTPException(status_code=500, detail="Failed to publish message.") from e
    else:
        # Handle different types of requests
        type = data_to_send.get("body", {}).get("configuration", {}).get("type")
        if type == "embedding":
            result = await embedding(data_to_send)
            return result
        if type == "image":
            result = await image(data_to_send)
            return result
        result = await chat_multiple_agents(data_to_send)
        return result

@router.post('/openai/responses', dependencies=[Depends(openai_sdk_middleware)])
async def openai_sdk_responses(request: Request, db_config: dict = Depends(add_configuration_data_to_body)):
    return await run_openai_chat_and_format(request, db_config, chat_completion)

@router.post("/playground/chat/completion/{bridge_id}", dependencies=[Depends(auth_and_rate_limit)])
async def playground_chat_completion_bridge(
    request: Request, db_config: dict = Depends(add_configuration_data_to_body)
):
    request.state.is_playground = True
    request.state.version = 2
    data_to_send = await make_request_data(request)

    message_id = str(uuid.uuid1())
    data_to_send["body"]["message_id"] = message_id

    org_id = data_to_send["state"]["profile"]["org"]["id"]
    bridge_id = data_to_send.get("body", {}).get("bridge_id")
    version_id = data_to_send.get("body", {}).get("version_id")
    channel_id = f"{org_id}_{bridge_id}_{version_id}"
    flag = data_to_send.get("body", {}).get("flag") or False
    if not flag:
        response_format = {"type": "RTLayer", "cred": {"channel": channel_id, "ttl": 1, "apikey": Config.RTLAYER_AUTH}}
        data_to_send["body"]["configuration"]["response_format"] = response_format
    # Check if response_format is present and publish to queue
    if not flag and response_format and response_format.get("type") != "default":
        try:
            # Publish the message to the queue
            data_to_send["body"]["bridge_configurations"]["playground_response_format"] = response_format
            await queue_obj.publish_message(data_to_send)
            return {"success": True, "message_id": message_id, "message": "Your response will be sent through configured means."}
        except Exception as e:
            # Log the error and return a meaningful error response
            logger.error(f"Failed to publish message: {str(e)}")
            raise HTTPException(status_code=500, detail="Failed to publish message.") from e
    else:
        type = data_to_send.get("body", {}).get("configuration", {}).get("type")
        if type == "embedding":
            result = await embedding(data_to_send)
            return result
        result = await chat_multiple_agents(data_to_send)
        return result


@router.websocket("/workflow/ws/{run_id}")
async def workflow_ws(websocket: WebSocket, run_id: str):
    from workflow.runner import HUMAN_INPUT_QUEUES, WORKFLOW_SESSIONS, WS_CONNECTIONS
    await websocket.accept()
    if run_id not in WORKFLOW_SESSIONS:
        await websocket.close(code=4004, reason="run_id not found")
        return

    WS_CONNECTIONS[run_id] = websocket
    try:
        while True:
            data = await websocket.receive_json()
            if data.get("action") != "answer":
                continue
            # Poll up to 5s for the queue to be ready (interrupt may arrive slightly after frontend sends)
            for _ in range(50):
                if run_id in HUMAN_INPUT_QUEUES:
                    break
                await asyncio.sleep(0.1)
            queue = HUMAN_INPUT_QUEUES.get(run_id)
            if queue:
                await queue.put(data.get("answer"))
            else:
                await websocket.send_json({"status": "error", "message": "workflow not waiting for input"})
    except WebSocketDisconnect:
        pass
    finally:
        WS_CONNECTIONS.pop(run_id, None)


@router.post("/batch/chat/completion", dependencies=[Depends(auth_and_rate_limit)])
async def batch_chat_completion(request: Request, db_config: dict = Depends(add_configuration_data_to_body)):
    data_to_send = await make_request_data(request)
    result = await batch(data_to_send)
    return result


@router.post("/testcases", dependencies=[Depends(auth_and_rate_limit)])
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
        org_id = request.state.profile["org"]["id"]

        # Execute testcases using the service
        from src.services.testcase_service import TestcaseNotFoundError, TestcaseValidationError, execute_testcases

        result = await execute_testcases(body, org_id)
        return result

    except TestcaseValidationError as ve:
        raise HTTPException(status_code=400, detail={"success": False, "error": str(ve)}) from ve
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
        raise HTTPException(
            status_code=500, detail={"success": False, "error": f"Internal server error: {str(e)}"}
        ) from e
