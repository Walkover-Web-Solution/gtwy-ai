import traceback

from fastapi.responses import JSONResponse

from src.db_services.testcase_services import (
    create_testcase,
    delete_testcase_by_id,
    get_merged_testcases_and_history_by_bridge_id,
    get_testcase_by_id,
    update_testcase_by_id,
)
from src.services.cache_service import make_json_serializable


async def create_testcase_controller(request):
    """Create a new testcase"""
    try:
        body = await request.json()

        # Validate required fields
        required_fields = ["bridge_id", "conversation", "type", "expected", "matching_type"]
        for field in required_fields:
            if field not in body:
                return JSONResponse(
                    status_code=400, content={"success": False, "error": f"Missing required field: {field}"}
                )

        result = await create_testcase(body)

        return JSONResponse(
            content={
                "success": True,
                "data": {"_id": str(result.inserted_id), "message": "Testcase created successfully"},
            }
        )
    except Exception as error:
        traceback.print_exc()
        return JSONResponse(status_code=400, content={"success": False, "error": str(error)})


async def delete_testcase_controller(testcase_id):
    """Delete a testcase by _id"""
    try:
        result = await delete_testcase_by_id(testcase_id)

        if result.deleted_count == 0:
            return JSONResponse(status_code=404, content={"success": False, "error": "Testcase not found"})

        return JSONResponse(content={"success": True, "message": "Testcase deleted successfully"})
    except Exception as error:
        traceback.print_exc()
        return JSONResponse(status_code=400, content={"success": False, "error": str(error)})


async def get_all_testcases_controller(bridge_id):
    """Get all testcases with their history merged for a specific bridge_id"""
    try:
        # Get merged data from both testcases and testcases_history collections
        merged_testcases = await get_merged_testcases_and_history_by_bridge_id(bridge_id)

        # Group history by version_id for each testcase
        for testcase in merged_testcases:
            testcase["version_history"] = {}
            for history in testcase["history"]:
                version_id = history["version_id"]
                if not testcase["version_history"].get(version_id):
                    testcase["version_history"][version_id] = []
                testcase["version_history"][version_id].append(history)
            # Remove the original history array as it's now organized by version_id
            del testcase["history"]

        return JSONResponse(content={"success": True, "data": make_json_serializable(merged_testcases)})
    except Exception as error:
        traceback.print_exc()
        return JSONResponse(status_code=400, content={"success": False, "error": str(error)})


async def handle_playground_testcase(result, parsed_data, Flag):
    """Handle testcase data from playground - create or update testcase"""
    try:
        # Extract expected response from result
        testcase_data = parsed_data["testcase_data"]
        expected_response = result.get("response", {}).get("data", {}).get("content", "")
        user = parsed_data["user"]

        # Check if testcase_id is present for update
        if testcase_data.get("testcase_id") and not Flag:
            # Update existing testcase
            testcase_id = testcase_data["testcase_id"]

            # Get existing testcase
            existing_testcase = await get_testcase_by_id(testcase_id)
            if not existing_testcase:
                return None  # Return None if testcase not found

            # Prepare update data
            update_data = {}
            if "conversation" in parsed_data["configuration"]:
                update_data["conversation"] = parsed_data["configuration"]["conversation"]
            if "matching_type" in testcase_data:
                update_data["matching_type"] = testcase_data["matching_type"]

            # Always update expected with current response
            update_data["expected"] = {"response": expected_response}
            update_data["conversation"].append({"role": "user", "content": user})

            # Update the testcase
            await update_testcase_by_id(testcase_id, update_data)

            return testcase_id  # Return existing testcase_id

        else:
            # Create new testcase with default values
            conversation = testcase_data.get("conversation", [])
            conversation.append({"role": "user", "content": user})

            new_testcase = {
                "bridge_id": parsed_data.get("bridge_id", ""),
                "conversation": conversation,
                "type": testcase_data.get("type", "response"),
                "expected": {"response": expected_response},
                "matching_type": testcase_data.get("matching_type", "exact"),
            }

            # If testcase_id is provided in testcase_data (for pre-generated IDs), use it
            if "testcase_id" in testcase_data and testcase_data["testcase_id"]:
                from bson import ObjectId

                new_testcase["_id"] = ObjectId(testcase_data["testcase_id"])
                await create_testcase(new_testcase)
                return testcase_data["testcase_id"]
            else:
                # Create the testcase without specific ID (MongoDB will auto-generate)
                result_insert = await create_testcase(new_testcase)
                return str(result_insert.inserted_id)

    except Exception:
        traceback.print_exc()
        return None  # Return None on error
