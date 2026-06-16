"""
Testcase Service Module

This module provides functions for handling testcase operations including:
- Validation of testcase data
- Fetching testcases from MongoDB or direct input
- Processing individual testcases
- Running testcases in parallel
"""

import asyncio
import copy
import json
import logging
from typing import Any

from bson import ObjectId

from config import Config
from models.mongo_connection import db
from src.services.commonServices.baseService.utils import send_message
from src.services.commonServices.common import chat
from src.services.utils.getConfiguration import getConfiguration

logger = logging.getLogger(__name__)


def build_rtlayer_cred(channel_id: str) -> dict[str, Any]:
    """Build RTLayer credentials for a given channel."""
    return {"channel": channel_id, "ttl": 1, "apikey": Config.RTLAYER_AUTH}


def _json_safe(value: Any) -> Any:
    """Recursively convert datetime/ObjectId and other non-JSON types to serializable forms."""
    from datetime import date, datetime

    if isinstance(value, dict):
        return {k: _json_safe(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_json_safe(v) for v in value]
    if isinstance(value, tuple):
        return [_json_safe(v) for v in value]
    if isinstance(value, ObjectId):
        return str(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return value


async def _publish_event(rtlayer_cred: dict[str, Any] | None, event: str, payload: dict[str, Any]) -> None:
    """Publish an event to RTLayer. Failures are logged and swallowed so they don't abort the run."""
    if not rtlayer_cred:
        return
    try:
        await send_message(cred=rtlayer_cred, data=_json_safe({"event": event, **payload}))
    except Exception as e:
        logger.error(f"Failed to publish '{event}' to RTLayer channel {rtlayer_cred.get('channel')}: {str(e)}")


class TestcaseValidationError(Exception):
    """Custom exception for testcase validation errors"""

    pass


class TestcaseNotFoundError(Exception):
    """Custom exception for testcase not found errors"""

    pass


def validate_testcase_request_data(body: dict[str, Any]) -> dict[str, Any]:
    """
    Validate and extract testcase request data

    Args:
        body: Request body dictionary

    Returns:
        Dictionary containing validated request parameters

    Raises:
        TestcaseValidationError: If required fields are missing or invalid
    """
    version_ids = body.get("version_ids")
    if not version_ids:
        raise TestcaseValidationError("version_ids is required")
    if not isinstance(version_ids, list):
        version_ids = [version_ids]

    bridge_id = body.get("bridge_id")
    testcase_id = body.get("testcase_id")
    testcases_flag = body.get("testcases", False)
    testcase_data = body.get("testcase_data")
    variables = body.get("variables", {})
    matching_type = body.get("matching_type", None)
    model_override = body.get("model")
    service_override = body.get("service")

    return {
        "bridge_id": bridge_id,
        "version_ids": version_ids,
        "testcase_id": testcase_id,
        "testcases_flag": testcases_flag,
        "testcase_data": testcase_data,
        "variables": variables,
        "matching_type": matching_type,
        "model_override": model_override,
        "service_override": service_override,
    }


def validate_direct_testcase_data(testcase_data: dict[str, Any]) -> None:
    """
    Validate direct testcase data fields

    Args:
        testcase_data: Dictionary containing testcase data

    Raises:
        TestcaseValidationError: If required fields are missing
    """
    required_fields = ["conversation", "expected", "matching_type"]

    for field in required_fields:
        if field not in testcase_data:
            raise TestcaseValidationError(f"{field} is required in testcase_data")


async def fetch_testcases_from_request(
    testcases_flag: bool, testcase_data: dict[str, Any] | None, bridge_id: str | None, testcase_id: str | None = None
) -> list[dict[str, Any]]:
    """
    Fetch testcases either from direct input or MongoDB

    Args:
        testcases_flag: Flag indicating if testcase data is provided directly
        testcase_data: Direct testcase data (if provided)
        bridge_id: Bridge ID for MongoDB query
        testcase_id: Specific testcase ID for MongoDB query (single)

    Returns:
        List of testcase dictionaries

    Raises:
        TestcaseValidationError: If validation fails
        TestcaseNotFoundError: If no testcases are found
    """
    if testcases_flag and testcase_data:
        # Validate direct testcase data
        validate_direct_testcase_data(testcase_data)

        # Create testcase object from direct input
        testcase = {
            "_id": "direct_testcase",
            "bridge_id": bridge_id,
            "conversation": testcase_data.get("conversation", []),
            "expected": testcase_data.get("expected", {}),
            "matching_type": testcase_data.get("matching_type", "cosine"),
            "type": "response",
        }

        return [testcase]

    # Fetch from MongoDB
    if not bridge_id:
        raise TestcaseValidationError("bridge_id is required")

    testcases_collection = db["testcases"]

    if testcase_id:
        testcase = await testcases_collection.find_one({"_id": ObjectId(testcase_id)})
        if not testcase:
            raise TestcaseNotFoundError("No testcase found for the given testcase_id")
        return [testcase]

    # No testcase_id -> fetch all testcases for bridge_id
    testcases = await testcases_collection.find({"bridge_id": bridge_id}).to_list(length=None)
    if not testcases:
        raise TestcaseNotFoundError("No testcases found for the given bridge_id")
    return testcases


async def get_testcase_configuration(
    org_id: str, version_id: str, bridge_id: str | None, testcases_flag: bool, testcase_data: dict[str, Any] | None, variables: dict[str, Any] | None
) -> dict[str, Any]:
    """
    Get configuration for testcase execution

    Args:
        org_id: Organization ID
        version_id: Version ID
        bridge_id: Bridge ID
        testcases_flag: Flag indicating direct testcase data
        testcase_data: Direct testcase data

    Returns:
        Configuration dictionary

    Raises:
        TestcaseValidationError: If configuration retrieval fails
    """
    # For direct testcase data, bridge_id might be None
    config_bridge_id = bridge_id if not (testcases_flag and testcase_data) else None

    db_config = await getConfiguration(
        None,
        None,
        config_bridge_id,
        None,
        None,
        variables,
        org_id,
        None,
        version_id=version_id,
        extra_tools=[],
        built_in_tools=None,
        guardrails=None,
    )

    if not db_config.get("success"):
        raise TestcaseValidationError(db_config.get("error", "Failed to get configuration"))

    primary_bridge_id = db_config.get("primary_bridge_id")
    bridge_configurations = db_config.get("bridge_configurations", {})

    return bridge_configurations[primary_bridge_id]


async def process_single_testcase(
    testcase: dict[str, Any],
    db_config: dict[str, Any],
    override_matching_type: str | None,
    rtlayer_cred: dict[str, Any] | None = None,
    version_id: str | None = None,
) -> dict[str, Any]:
    """
    Process a single testcase

    Args:
        testcase: Testcase dictionary
        db_config: Configuration dictionary
        override_matching_type: Optional override matching type
        rtlayer_cred: Optional RTLayer credentials; when set, the result is pushed to the channel
        version_id: Optional version id, included in the RTLayer payload

    Returns:
        Dictionary containing testcase result
    """
    try:
        # Merge testcase-stored variables (higher priority) with config/request variables
        merged_variables = {**db_config.get("variables", {}), **testcase.get("variables", {})}
        db_config["variables"] = merged_variables

        # Set conversation in db_config
        db_config["configuration"]["conversation"] = testcase.get("conversation", [])

        # Force non-streaming for testcase execution so all versions return a
        # parseable JSONResponse regardless of the bridge's configured stream flag.
        db_config["configuration"]["stream"] = False

        # Create request data for this testcase
        testcase_request_data = {
            "body": {
                "user": testcase.get("conversation", [])[-1].get("content", "") if testcase.get("conversation") else "",
                "testcase_data": {
                    "matching_type": override_matching_type or testcase.get("matching_type") or "cosine",
                    "run_testcase": True,
                    "_id": testcase.get("_id"),
                    "expected": testcase.get("expected"),
                    "type": testcase.get("type", "response"),
                    "skip_testcase_creation": True,  # Don't create new testcases during execution
                },
                **db_config,
            },
            "state": {"version": 2},
        }

        # Call chat function
        result = await chat(testcase_request_data)

        # Extract data from JSONResponse object
        if hasattr(result, "body"):
            result_data = json.loads(result.body.decode("utf-8"))
        else:
            result_data = result

        # Detect non-exception error responses (e.g. JSONResponse with success=False)
        if isinstance(result_data, dict) and result_data.get("success") is False:
            err_msg = result_data.get("error") or "Unknown error from chat service"
            outcome = {
                "testcase_id": str(testcase.get("_id")) if testcase.get("_id") != "direct_testcase" else "direct_testcase",
                "bridge_id": testcase.get("bridge_id"),
                "expected": testcase.get("expected"),
                "actual_result": None,
                "score": 0,
                "matching_type": testcase.get("matching_type", "cosine"),
                "error": err_msg,
                "success": False,
            }
            await _publish_event(rtlayer_cred, "testcase_result", {"version_id": version_id, "result": outcome})
            return outcome

        # Extract testcase result with score if available
        testcase_result = (
            result_data.get("response", {}).get("testcase_result", {}) if isinstance(result_data, dict) else {}
        )
        
        # Extract tools_call_data from testcase_result (sourced from historyParams)
        tools_call_data = testcase_result.get("tools_call_data", []) if isinstance(testcase_result, dict) else []
        usage_data = result_data.get("response", {}).get("usage", {}) if isinstance(result_data, dict) else {}
        total_tokens = usage_data.get("total_tokens", 0)
        cost = usage_data.get("cost", 0)

        outcome = {
            "testcase_id": str(testcase.get("_id")) if testcase.get("_id") != "direct_testcase" else "direct_testcase",
            "bridge_id": testcase.get("bridge_id"),
            "expected": testcase.get("expected"),
            "actual_result": result_data.get("response", {}).get("data", {}).get("content", "")
            if isinstance(result_data, dict)
            else str(result_data),
            "score": testcase_result.get("score"),
            "matching_type": testcase_result.get("matching_type") or testcase.get("matching_type", ""),
            "success": True,
            "tools_call_data": tools_call_data,
            "total_tokens": total_tokens,
            "cost": cost,
        }
        await _publish_event(rtlayer_cred, "testcase_result", {"version_id": version_id, "result": outcome})
        return outcome

    except Exception as e:
        # chat() raises ValueError(error_object) where error_object is a dict
        # like {"success": False, "error": "...", "message_id": "..."}
        error_message = str(e)
        err_args = getattr(e, "args", None)
        if err_args and isinstance(err_args[0], dict):
            error_message = err_args[0].get("error") or error_message
        logger.error(f"Error processing testcase {testcase.get('_id')}: {error_message}")
        outcome = {
            "testcase_id": str(testcase.get("_id")) if testcase.get("_id") != "direct_testcase" else "direct_testcase",
            "bridge_id": testcase.get("bridge_id"),
            "expected": testcase.get("expected"),
            "actual_result": None,
            "score": 0,
            "matching_type": testcase.get("matching_type", "cosine"),
            "error": error_message,
            "success": False,
        }
        await _publish_event(rtlayer_cred, "testcase_result", {"version_id": version_id, "result": outcome})
        return outcome


async def run_testcases_parallel(
    testcases: list[dict[str, Any]],
    db_config: dict[str, Any],
    override_matching_type: str | None,
    rtlayer_cred: dict[str, Any] | None = None,
    version_id: str | None = None,
) -> list[dict[str, Any]]:
    """
    Run multiple testcases in parallel.

    Args:
        testcases: List of testcase dictionaries
        db_config: Configuration dictionary
        override_matching_type: Optional override matching type
        rtlayer_cred: Optional RTLayer credentials for streaming results
        version_id: Version id, included in published payloads

    Returns:
        List of testcase results
    """
    results = await asyncio.gather(
        *[
            process_single_testcase(tc, copy.deepcopy(db_config), override_matching_type, rtlayer_cred, version_id)
            for tc in testcases
        ]
    )

    try:
        from src.db_services.testcase_services import update_testcase_last_executed
        executed_ids = [
            tc.get("_id") for tc in testcases
            if tc.get("_id") and tc.get("_id") != "direct_testcase"
        ]
        if executed_ids:
            await update_testcase_last_executed(executed_ids)
    except Exception as e:
        logger.error(f"Failed to update lastExecutedAt: {str(e)}")

    return results


async def execute_testcases(
    body: dict[str, Any],
    org_id: str,
    rtlayer_cred: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """
    Main function to execute testcases end-to-end

    Args:
        body: Request body dictionary
        org_id: Organization ID
        rtlayer_cred: Optional RTLayer credentials. When provided, per-testcase and
            lifecycle events are streamed to the channel as the run progresses.

    Returns:
        Dictionary containing execution results

    Raises:
        TestcaseValidationError: If validation fails
        TestcaseNotFoundError: If testcases are not found
    """
    # Validate request data
    request_data = validate_testcase_request_data(body)

    # Fetch testcases
    testcases = await fetch_testcases_from_request(
        request_data["testcases_flag"],
        request_data["testcase_data"],
        request_data["bridge_id"],
        request_data["testcase_id"],
    )

    version_ids = request_data["version_ids"]

    await _publish_event(
        rtlayer_cred,
        "run_started",
        {
            "bridge_id": request_data["bridge_id"],
            "version_ids": version_ids,
            "total_testcases": len(testcases),
        },
    )

    async def run_for_version(version_id):
        db_config = await get_testcase_configuration(
            org_id,
            version_id,
            request_data["bridge_id"],
            request_data["testcases_flag"],
            request_data["testcase_data"],
            request_data["variables"],
        )
        model_override = request_data.get("model_override")
        service_override = request_data.get("service_override")
        if service_override:
            db_config["service"] = service_override.lower()
        if model_override:
            db_config.setdefault("configuration", {})["model"] = model_override

        results = await run_testcases_parallel(
            testcases,
            db_config,
            request_data["matching_type"],
            rtlayer_cred=rtlayer_cred,
            version_id=version_id,
        )
        model = db_config.get("configuration", {}).get("model")
        service_name = db_config.get("service") 
        tools_call_data = []
        if results and isinstance(results[0], dict):
            tools_call_data = results[0].get("tools_call_data", [])
        total_tokens = 0
        total_cost = 0
        for result in results:
            if isinstance(result, dict):
                total_tokens += result.get("total_tokens", 0)
                total_cost += result.get("cost", 0)
                        
        return {
            "version_id": version_id,
            "total_testcases": len(testcases),
            "results": results,
            "model": model,
            "service_name": service_name,
            "tools_call_data": tools_call_data,
            "total_tokens": total_tokens,
            "cost": total_cost,
        }

    # Run all versions concurrently (works for 1 or N)
    version_results = await asyncio.gather(*[run_for_version(vid) for vid in version_ids])
    final_payload = {
        "success": True,
        "bridge_id": request_data["bridge_id"],
        "version_ids": version_ids,
        "total_versions": len(version_ids),
        "version_results": version_results,
        "testcase_source": "direct"
        if (request_data["testcases_flag"] and request_data["testcase_data"])
        else "mongodb",
    }
    await _publish_event(rtlayer_cred, "run_completed", final_payload)
    return final_payload
