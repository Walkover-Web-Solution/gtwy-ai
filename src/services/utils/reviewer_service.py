"""
Reviewer Agent Service

After the main agent produces a response, if a reviewer_agent is configured
in the bridge settings (as {"bridge_id": "<reviewer_bridge_id>"}), this service:
  1. Calls the reviewer bridge via the existing chat() flow, passing user message,
     main agent response, and purpose as the user input.
  2. The reviewer bridge must be configured to return JSON: {"approved": bool, "review": str}
  3. If rejected and retries < max_retries (3), injects the review back into the
     main agent's conversation as a new user turn and re-runs the main agent.
  4. If approved (or max retries exhausted), returns the final result.
"""

import json
import traceback
import uuid

from globals import logger


MAX_REVIEWER_RETRIES = 3


def _build_reviewer_user_message(user_message: str, agent_response: str) -> str:
    return (
        f"User Message:\n{user_message}\n\n"
        f"Agent Response:\n{agent_response}"
    )


async def _call_reviewer_bridge(reviewer_bridge_id: str, user_message: str, agent_response: str, purpose: str, request_body: dict, bridge_configurations: dict, reviewer_thread_id: str = None) -> dict:
    """
    Calls the reviewer bridge via chat() and returns {"approved": bool, "review": str}.
    """
    from src.services.commonServices.common import chat
    from src.services.utils.getConfiguration import getConfiguration

    reviewer_input = _build_reviewer_user_message(user_message, agent_response)

    original_body = request_body.get("body", {})
    state = request_body.get("state", {})
    org_id = state.get("profile", {}).get("org", {}).get("id", "") or original_body.get("org_id", "")

    db_config = await getConfiguration(
        configuration=None,
        service=None,
        bridge_id=reviewer_bridge_id,
        apikey=original_body.get("apikey"),
        org_id=org_id,
    )

    reviewer_bridge_config = db_config.get("bridge_configurations", {}).get(reviewer_bridge_id, {})

    reviewer_body = {}
    reviewer_body.update(reviewer_bridge_config)
    reviewer_body["bridge_id"] = reviewer_bridge_id
    reviewer_body["user"] = reviewer_input
    reviewer_body["bridge_configurations"] = {}
    reviewer_body["org_id"] = org_id
    if reviewer_thread_id:
        reviewer_body["thread_id"] = reviewer_thread_id
    state["timer"] = state.get("timer")
    reviewer_request = {
        "body": reviewer_body,
        "state": state.copy(),
        "path_params": {"bridge_id": reviewer_bridge_id},
    }

    raw = await chat(reviewer_request)

    if hasattr(raw, "body"):
        response_data = json.loads(raw.body.decode("utf-8"))
    else:
        response_data = raw

    content = response_data.get("response", {}).get("data", {}).get("content", "")

    try:
        parsed = json.loads(content)
    except (json.JSONDecodeError, TypeError):
        import re
        match = re.search(r'\{.*\}', content or "", re.DOTALL)
        parsed = json.loads(match.group()) if match else {"approved": True, "review": "Could not parse reviewer response; approving by default."}

    return {
        "approved": bool(parsed.get("approved", True)),
        "review": str(parsed.get("review", "")),
    }


async def run_reviewer_loop(
    result: dict,
    parsed_data: dict,
    params: dict,
    class_obj,
    handle_response_caching_fn,
    Helper,
    request_body: dict = None,
    bridge_configurations: dict = None,
) -> dict:
    """
    Main entry point. Called after the initial execute() succeeds.
    """
    settings = parsed_data.get("settings") or {}
    # reviewer_config = {
    #     "is_enabled" : True, 
    #     "bridge_id" : "69e777216739f72ee0f46ca3"
    # } if "669e105ea0a71e952fc8251f" in bridge_configurations else None
    reviewer_config = settings.get("reviewer_agent")
    
    if not reviewer_config or not reviewer_config.get("is_enabled", False):
        return result

    reviewer_agent_id = reviewer_config.get("agent_id")
    if not reviewer_agent_id:
        logger.warning("[ReviewerAgent] reviewer_agent is enabled but no agent_id configured. Skipping.")
        return result

    purpose = parsed_data.get("configuration", {}).get("prompt", "") or ""
    user_message = parsed_data.get("original_user") or parsed_data.get("user") or ""

    # main_thread_id = parsed_data.get("thread_id") or ""
    reviewer_thread_id =  f"reviewer_{uuid.uuid4().hex}"

    for attempt in range(MAX_REVIEWER_RETRIES):
        agent_response_text = result.get("response", {}).get("data", {}).get("content", "")

        logger.info(f"[ReviewerAgent] Attempt {attempt + 1}/{MAX_REVIEWER_RETRIES} — calling reviewer bridge '{reviewer_agent_id}'.")

        try:
            review_result = await _call_reviewer_bridge(
                reviewer_bridge_id=reviewer_agent_id,
                user_message=user_message,
                agent_response=agent_response_text,
                purpose=purpose,
                request_body=request_body or {},
                bridge_configurations=bridge_configurations or {},
                reviewer_thread_id=reviewer_thread_id,
            )
        except Exception as exc:
            logger.error(f"[ReviewerAgent] Reviewer call failed: {exc}\n{traceback.format_exc()}")
            break

        approved = review_result.get("approved", True)
        review_text = review_result.get("review", "")

        logger.info(f"[ReviewerAgent] approved={approved}, review='{review_text}'")

        if approved:
            logger.info("[ReviewerAgent] Response approved.")
            break

        if attempt >= MAX_REVIEWER_RETRIES - 1:
            logger.info("[ReviewerAgent] Max retries reached; using last response.")
            break

        logger.info(f"[ReviewerAgent] Rejected. Injecting review into conversation for retry {attempt + 2}.")

        _inject_review_into_conversation(parsed_data, agent_response_text, review_text)

        try:
            new_class_obj = await Helper.create_service_handler(params, parsed_data["service"])
            result = await handle_response_caching_fn(parsed_data=parsed_data, class_obj=new_class_obj)
        except Exception as exc:
            logger.error(f"[ReviewerAgent] Re-run failed: {exc}\n{traceback.format_exc()}")
            break

    return result


def _inject_review_into_conversation(parsed_data: dict, agent_response: str, review_text: str):
    """
    Appends the agent's previous response and the reviewer's feedback into the
    conversation history so the main agent sees them in the next call.
    """
    conversation = parsed_data.get("configuration", {}).get("conversation") or []

    conversation.append({"role": "assistant", "content": agent_response})
    conversation.append({
        "role": "user",
        "content": (
            f"Your previous response was reviewed and rejected. "
            f"Please improve it based on the following feedback:\n\n{review_text}"
        ),
    })

    parsed_data["configuration"]["conversation"] = conversation
