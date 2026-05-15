"""
Testcase Service Module

This module provides functions for handling testcase operations including:
- Validation of testcase data
- Fetching testcases from MongoDB or direct input
- Processing individual testcases
- Running testcases in parallel
"""

import asyncio
import json
import logging
from typing import Any

from bson import ObjectId

from models.mongo_connection import db
from src.services.commonServices.common import chat
from src.services.utils.getConfiguration import getConfiguration

logger = logging.getLogger(__name__)


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

    return {
        "bridge_id": bridge_id,
        "version_ids": version_ids,
        "testcase_id": testcase_id,
        "testcases_flag": testcases_flag,
        "testcase_data": testcase_data,
        "variables": variables,
        "matching_type": matching_type,
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


async def process_single_testcase(testcase: dict[str, Any], db_config: dict[str, Any], override_matching_type: str | None) -> dict[str, Any]:
    """
    Process a single testcase

    Args:
        testcase: Testcase dictionary
        db_config: Configuration dictionary

    Returns:
        Dictionary containing testcase result
    """
    try:
        # Deep copy the configuration to avoid race conditions in parallel execution
        if "configuration" in db_config:
            db_config["configuration"] = db_config["configuration"].copy()

        # Merge testcase-stored variables (higher priority) with config/request variables
        merged_variables = {**db_config.get("variables", {}), **testcase.get("variables", {})}
        db_config["variables"] = merged_variables

        # Set conversation in db_config
        db_config["configuration"]["conversation"] = testcase.get("conversation", [])

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

        # Extract testcase result with score if available
        testcase_result = (
            result_data.get("response", {}).get("testcase_result", {}) if isinstance(result_data, dict) else {}
        )

        return {
            "testcase_id": str(testcase.get("_id")) if testcase.get("_id") != "direct_testcase" else "direct_testcase",
            "bridge_id": testcase.get("bridge_id"),
            "expected": testcase.get("expected"),
            "actual_result": result_data.get("response", {}).get("data", {}).get("content", "")
            if isinstance(result_data, dict)
            else str(result_data),
            "score": testcase_result.get("score"),
            "matching_type": testcase_result.get("matching_type") or testcase.get("matching_type", ""),
            "success": True,
        }

    except Exception as e:
        logger.error(f"Error processing testcase {testcase.get('_id')}: {str(e)}")
        return {
            "testcase_id": str(testcase.get("_id")) if testcase.get("_id") != "direct_testcase" else "direct_testcase",
            "bridge_id": testcase.get("bridge_id"),
            "expected": testcase.get("expected"),
            "actual_result": None,
            "score": 0,
            "matching_type": testcase.get("matching_type", "cosine"),
            "error": str(e),
            "success": False,
        }


def _filter_testcases_to_run(
    testcases: list[dict[str, Any]],
    version_updated_at,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Split testcases into (to_run, skipped) based on lastExecutedAt vs updatedAt.

    Skip when both version.updatedAt <= lastExecutedAt and
    testcase.updatedAt <= lastExecutedAt.
    """
    if not version_updated_at:
        return testcases, []

    to_run: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    for tc in testcases:
        tc_updated_at = tc.get("updatedAt") or tc.get("updated_at")
        execution = tc.get("execution") or {}
        last_executed_at = execution.get("lastExecutedAt")

        if (
            last_executed_at
            and tc_updated_at
            and version_updated_at <= last_executed_at
            and tc_updated_at <= last_executed_at
        ):
            skipped.append({
                "testcase_id": str(tc.get("_id")),
                "bridge_id": tc.get("bridge_id"),
                "skipped": True,
                "reason": "no_changes_since_last_execution",
                "last_executed_at": last_executed_at,
            })
        else:
            to_run.append(tc)
    return to_run, skipped


async def run_testcases_parallel(
    testcases: list[dict[str, Any]],
    db_config: dict[str, Any],
    override_matching_type: str | None,
    version_updated_at=None,
) -> list[dict[str, Any]]:
    """
    Run multiple testcases in parallel

    Args:
        testcases: List of testcase dictionaries
        db_config: Configuration dictionary
        override_matching_type: Optional override matching type
        version_updated_at: Optional version updatedAt timestamp (for skip logic)

    Returns:
        List of testcase results (executed + skipped)
    """
    # Filter out testcases that don't need rerun (version & testcase unchanged)
    to_run, skipped = _filter_testcases_to_run(testcases, version_updated_at)

    # Process pending testcases in parallel
    results = await asyncio.gather(
        *[process_single_testcase(tc, db_config.copy(), override_matching_type) for tc in to_run]
    )

    # Update execution.lastExecutedAt for all executed testcases (skip direct_testcase)
    try:
        from src.db_services.testcase_services import update_testcase_last_executed
        executed_ids = [
            tc.get("_id") for tc in to_run
            if tc.get("_id") and tc.get("_id") != "direct_testcase"
        ]
        if executed_ids:
            await update_testcase_last_executed(executed_ids)
    except Exception as e:
        logger.error(f"Failed to update lastExecutedAt: {str(e)}")

    return results + skipped


async def execute_testcases(body: dict[str, Any], org_id: str) -> dict[str, Any]:
    """
    Main function to execute testcases end-to-end

    Args:
        body: Request body dictionary
        org_id: Organization ID

    Returns:
        Dictionary containing execution results

    Raises:
        TestcaseValidationError: If validation fails
        TestcaseNotFoundError: If testcases are not found
    """
    from src.db_services.testcase_services import get_version_updated_at

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

    async def run_for_version(version_id):
        db_config, version_updated_at = await asyncio.gather(
            get_testcase_configuration(
                org_id,
                version_id,
                request_data["bridge_id"],
                request_data["testcases_flag"],
                request_data["testcase_data"],
                request_data["variables"],
            ),
            get_version_updated_at(version_id),
        )
        results = await run_testcases_parallel(
            testcases,
            db_config,
            request_data["matching_type"],
            version_updated_at=version_updated_at,
        )
        return {
            "version_id": version_id,
            "total_testcases": len(testcases),
            "results": results,
        }

    # Run all versions concurrently (works for 1 or N)
    version_results = await asyncio.gather(*[run_for_version(vid) for vid in version_ids])

    return {
        "success": True,
        "bridge_id": request_data["bridge_id"],
        "version_ids": version_ids,
        "total_versions": len(version_ids),
        "version_results": version_results,
        "testcase_source": "direct"
        if (request_data["testcases_flag"] and request_data["testcase_data"])
        else "mongodb",
    }
