from fastapi import HTTPException, Query

from globals import logger
from src.configs.constant import bridge_ids
from src.services.prebuilt_prompt_service import get_specific_prebuilt_prompt_service
from src.services.utils.gpt_memory import get_gpt_memory

from ..ai_call_util import call_ai_middleware


async def structured_output_optimizer(request):
    try:
        body = await request.json()
        variables = {"json_schema": body.get("json_schema"), "query": body.get("query")}
        thread_id = body.get("thread_id") or None
        org_id = request.state.profile.get("org", {}).get("id", "")
        user = "create the json shcmea accroding to the dummy json explained in system prompt."
        configuration = None
        updated_prompt = await get_specific_prebuilt_prompt_service(org_id, "structured_output_optimizer")
        if updated_prompt and updated_prompt.get("structured_output_optimizer"):
            configuration = {"prompt": updated_prompt["structured_output_optimizer"]}
        result = await call_ai_middleware(
            user,
            bridge_id=bridge_ids["structured_output_optimizer"],
            configuration=configuration,
            variables=variables,
            thread_id=thread_id,
        )
        return result
    except Exception as err:
        logger.error("Error calling function structured_output_optimizer=>", err)
        return None


async def retrieve_gpt_memory(
    bridge_id: str = Query(..., min_length=1),
    thread_id: str = Query(..., min_length=1),
    sub_thread_id: str = Query(..., min_length=1),
    version_id: str | None = Query(None),
):
    bridge_id = bridge_id.strip()
    thread_id = thread_id.strip()
    sub_thread_id = sub_thread_id.strip()
    version_id = version_id.strip() if version_id else None

    if not bridge_id or not thread_id or not sub_thread_id:
        raise HTTPException(status_code=400, detail="bridge_id, thread_id and sub_thread_id are required")

    memory_id, memory = await get_gpt_memory(
        bridge_id=bridge_id, thread_id=thread_id, sub_thread_id=sub_thread_id, version_id=version_id
    )

    return {
        "bridge_id": bridge_id,
        "thread_id": thread_id,
        "sub_thread_id": sub_thread_id,
        "version_id": version_id,
        "memory_id": memory_id,
        "found": memory is not None,
        "memory": memory,
    }


async def improve_prompt_optimizer(request):
    try:
        body = await request.json()
        variables = body.get("variables")
        user = "improve the prompt"
        result = await call_ai_middleware(user, bridge_id=bridge_ids["improve_prompt_optimizer"], variables=(variables))
        return result
    except Exception as err:
        logger.error("Error Calling function prompt optimise", err)
