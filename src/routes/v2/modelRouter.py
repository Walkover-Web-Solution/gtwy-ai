import asyncio
from concurrent.futures import ThreadPoolExecutor
import uuid
from fastapi import APIRouter, Depends, HTTPException, Request

from config import Config
from globals import logger
from src.middlewares.ratelimitMiddleware import rate_limit
from src.services.commonServices.baseService.utils import make_request_data
from src.services.commonServices.common import batch, chat_multiple_agents, embedding, image
from src.services.commonServices.queueService.queueService import queue_obj
from src.services.utils.helper import queue_rerun_messages

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
    request.state.version = 2
    data_to_send = await make_request_data(request)
    is_playground = data_to_send.get("body", {}).get("is_playground", False)

    message_id = str(uuid.uuid1())
    data_to_send["body"]["message_id"] = message_id
    current_id = data_to_send.get('id_to_use','')
    
    if data_to_send['body']['configuration'].get('response_format') is not None:
        data_to_send['body']['settings']['response_format'] = data_to_send['body']['configuration']['response_format']
        del data_to_send['body']['configuration']['response_format']
    
    stream_config = db_config.get('bridge_configurations', {}).get(current_id, {}).get('configuration', {}).get('stream')
    if is_playground and (stream_config == False or stream_config is None or stream_config == 'default'):
        channel_id = f"{data_to_send.get('state', {}).get('profile', {}).get('org', {}).get('id')}_{db_config.get('bridge_configurations', {}).get(current_id, {}).get('bridge_id')}_{db_config.get('bridge_configurations', {}).get(current_id, {}).get('version_id')}"
        playground_response_format = {"type": "RTLayer", "cred": {"channel": channel_id, "ttl": 1, "apikey": Config.RTLAYER_AUTH}}
        data_to_send['body']['settings']['response_format'] = playground_response_format
    
    response_format = data_to_send.get("body", {}).get("settings", {}).get("response_format",{})
    is_webhook_playground = response_format and response_format.get("type") == "webhook" and is_playground
    if (response_format and response_format.get("type") != "default" and not is_webhook_playground):
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


@router.post("/batch/chat/completion", dependencies=[Depends(auth_and_rate_limit)])
async def batch_chat_completion(request: Request, db_config: dict = Depends(add_configuration_data_to_body)):
    data_to_send = await make_request_data(request)
    result = await batch(data_to_send)
    return result


@router.post("/testcases", dependencies=[Depends(auth_and_rate_limit)])
async def run_testcases_route(request: Request):
    """
    Execute testcases either from direct input or MongoDB

    Returns immediately and streams results to an RTLayer channel
    `{org_id}_{bridge_id}` as each testcase completes. Avoids HTTP timeouts
    on long testcase runs.

    Events published to the channel:
    - run_started        : { bridge_id, version_ids, total_testcases }
    - testcase_result    : { version_id, result }
    - version_completed  : { version_id, total_testcases, results }
    - run_completed      : full final payload
    - run_failed         : { error } on any unhandled execution error
    """
    request.state.version = 2

    from src.services.testcase_service import (
        TestcaseNotFoundError,
        TestcaseValidationError,
        build_rtlayer_cred,
        execute_testcases,
    )

    body = await request.json()
    org_id = request.state.profile["org"]["id"]
    body.setdefault("state", {})["profile"] = request.state.profile
    bridge_id = body.get("bridge_id")
    if not bridge_id:
        raise HTTPException(
            status_code=400,
            detail={"success": False, "error": "bridge_id is required for RTLayer-based testcase runs"},
        )

    channel_id = f"{org_id}_{bridge_id}"
    rtlayer_cred = build_rtlayer_cred(channel_id)

    async def _run():
        try:
            await execute_testcases(body, org_id, rtlayer_cred=rtlayer_cred)
        except TestcaseValidationError as ve:
            from src.services.commonServices.baseService.utils import send_message
            await send_message(cred=rtlayer_cred, data={"event": "run_failed", "error": str(ve)})
        except TestcaseNotFoundError as nfe:
            from src.services.commonServices.baseService.utils import send_message
            await send_message(cred=rtlayer_cred, data={"event": "run_completed", "success": True, "message": str(nfe), "results": []})
        except Exception as e:
            logger.error(f"Background testcase run failed for channel {channel_id}: {str(e)}")
            from src.services.commonServices.baseService.utils import send_message
            await send_message(cred=rtlayer_cred, data={"event": "run_failed", "error": f"Internal server error: {str(e)}"})

    asyncio.create_task(_run())

    return {
        "success": True,
        "channel_id": channel_id,
        "message": "Your response will be sent through configured means.",
    }


@router.post("/rerun", dependencies=[Depends(auth_and_rate_limit)])
async def rerun_messages_route(request: Request, db_config: dict = Depends(add_configuration_data_to_body)):
    """
    Rerun previous messages with current bridge configuration.
    Each message is pushed to the queue for async processing.

    Option 1 – by message IDs:
        Body: {"bridge_id": "...", "message_ids": ["msg-id-1", ...]}
    Option 2 – by thread (reruns the last message in the thread):
        Body: {"bridge_id": "...", "thread_id": "...", "sub_thread_id": "..."}
    """
    try:
        request.state.version = 2
        data_to_send = await make_request_data(request)

        body = data_to_send.get("body", {})
        org_id = data_to_send["state"]["profile"]["org"]["id"]

        thread_id = body.get("thread_id")
        sub_thread_id = body.get("sub_thread_id")
        message_ids = body.get("message_ids", [])
        is_thread_rerun = thread_id and sub_thread_id and not message_ids

        if not is_thread_rerun and (not message_ids or not isinstance(message_ids, list)):
            raise HTTPException(
                status_code=400,
                detail={"success": False, "error": "Provide either message_ids array or both thread_id and sub_thread_id"},
            )

        result = await queue_rerun_messages(
            data_to_send, queue_obj, org_id,
            message_ids=message_ids if not is_thread_rerun else None,
            bridge_id=body.get("bridge_id") if is_thread_rerun else None,
            thread_id=thread_id if is_thread_rerun else None,
            sub_thread_id=sub_thread_id if is_thread_rerun else None,
        )

        if not result["queued"]:
            raise HTTPException(
                status_code=404,
                detail={"success": False, "error": "No conversation found for the given parameters"},
            )

        response = {"success": True, "message": f"{len(result['queued'])} message(s) queued for rerun.", "queued": result["queued"]}
        if result["not_found"]:
            response["not_found"] = result["not_found"]
        if result["conversations"]:
            response["history_count"] = len(result["conversations"])
        return response

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error in rerun_messages_route: {str(e)}")
        raise HTTPException(status_code=500, detail={"success": False, "error": f"Internal server error: {str(e)}"}) from e
