import datetime
import traceback

from bson import ObjectId
from fastapi import HTTPException

from globals import logger
from models.mongo_connection import db

configurationModel = db["configurations"]
testcases_model = db["testcases"]
testcases_history_model = db["testcases_history"]


async def get_testcases(bridge_id):
    return await testcases_model.find({"bridge_id": bridge_id}).to_list(length=None)


async def create_testcase(testcase_data):
    result = await testcases_model.insert_one(testcase_data)
    return result


async def delete_testcase_by_id(testcase_id):
    """Delete a testcase by _id"""
    try:
        object_id = ObjectId(testcase_id)
        result = await testcases_model.delete_one({"_id": object_id})
        return result
    except Exception as e:
        logger.error(f"Error deleting testcase: {str(e)}")
        raise e


async def get_all_testcases_by_bridge_id(bridge_id):
    """Get all testcases for a specific bridge_id"""
    return await testcases_model.find({"bridge_id": bridge_id}).to_list(length=None)


async def get_testcase_by_id(testcase_id):
    """Get a testcase by _id"""
    try:
        object_id = ObjectId(testcase_id)
        return await testcases_model.find_one({"_id": object_id})
    except Exception as e:
        logger.error(f"Error fetching testcase: {str(e)}")
        raise e


async def update_testcase_by_id(testcase_id, update_data):
    """Update a testcase by _id"""
    try:
        object_id = ObjectId(testcase_id)
        update_data["updated_at"] = datetime.datetime.utcnow()
        result = await testcases_model.update_one({"_id": object_id}, {"$set": update_data})
        return result
    except Exception as e:
        logger.error(f"Error updating testcase: {str(e)}")
        raise e


async def create_testcases_history(data):
    result = await testcases_history_model.insert_many(data)
    for obj in data:
        obj["_id"] = str(obj["_id"])
    return result


async def get_merged_testcases_and_history_by_bridge_id(bridge_id):
    """Get all testcases with their history merged for a specific bridge_id"""
    try:
        # Use aggregation pipeline to merge testcases with their history
        pipeline = [
            {"$match": {"bridge_id": bridge_id}},
            {
                "$lookup": {
                    "from": "testcases_history",
                    "let": {"testcaseIdStr": {"$toString": "$_id"}},
                    "pipeline": [{"$match": {"$expr": {"$eq": ["$testcase_id", "$$testcaseIdStr"]}}}],
                    "as": "history",
                }
            },
        ]

        cursor = testcases_model.aggregate(pipeline)
        result = await cursor.to_list(length=None)
        return result
    except Exception as e:
        logger.error(f"Error fetching merged testcases and history: {str(e)}")
        raise e


async def delete_current_testcase_history(version_id):
    try:
        pipeline = [
            {"$match": {"version_id": version_id}},
            {"$sort": {"testcase_id": 1, "created_at": -1}},
            {"$group": {"_id": "$testcase_id", "latest_id": {"$first": "$_id"}}},
            {"$project": {"_id": 0, "latest_id": 1}},
        ]

        # Get IDs of latest entries
        cursor = testcases_history_model.aggregate(pipeline)
        latest_ids = [doc["latest_id"] async for doc in cursor]

        # Delete all other entries for the given version_id
        await testcases_history_model.delete_many({"version_id": version_id, "_id": {"$nin": latest_ids}})
    except Exception as e:
        logger.error(f"Error in deleting current testcase history: {str(e)}")
        traceback.print_exc()


async def parse_and_save_testcases(testcases_data, bridge_id: str):
    """Process AI-generated test cases and save them to database"""
    saved_testcase_ids = []

    try:
        test_cases = testcases_data.get("test_cases", [])
        if not test_cases:
            return saved_testcase_ids

        # Convert dict with numbered keys to list
        if isinstance(test_cases, dict) and all(key.isdigit() for key in test_cases.keys()):
            test_cases = [test_cases[key] for key in sorted(test_cases.keys(), key=int)]

        for i, test_case in enumerate(test_cases, 1):
            try:
                user_input = test_case.get("UserInput")
                expected_output = test_case.get("ExpectedOutput")

                if not user_input or not expected_output:
                    logger.warning(f"Skipping test case {i}: missing UserInput or ExpectedOutput")
                    continue

                # Convert dict expected_output to string
                if isinstance(expected_output, dict):
                    expected_output = str(expected_output)

                testcase_data = {
                    "bridge_id": bridge_id,
                    "conversation": [{"role": "user", "content": str(user_input)}],
                    "type": "response",
                    "expected": {"response": str(expected_output)},
                    "matching_type": "contains",
                }

                result = await create_testcase(testcase_data)
                saved_testcase_ids.append(str(result.inserted_id))
                logger.info(f"Saved test case {i} with ID: {result.inserted_id}")

            except Exception as case_error:
                logger.error(f"Error processing test case {i}: {str(case_error)}")
                continue

    except Exception as e:
        logger.error(f"Error processing test cases: {str(e)}")
        raise HTTPException(
            status_code=500, detail={"success": False, "error": f"Error processing test cases: {str(e)}"}
        ) from e

    return saved_testcase_ids
