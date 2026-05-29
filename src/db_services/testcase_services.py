import datetime

from bson import ObjectId
from fastapi import HTTPException

from globals import logger
from models.mongo_connection import db

configurationModel = db["configurations"]
testcases_model = db["testcases"]


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


async def update_testcase_last_executed(testcase_ids):
    """Update execution.lastExecutedAt for the given testcase ids"""
    try:
        if not testcase_ids:
            return None
        # Filter out non-ObjectId entries (e.g. 'direct_testcase')
        object_ids = []
        for tid in testcase_ids:
            try:
                object_ids.append(ObjectId(tid))
            except Exception:
                continue
        if not object_ids:
            return None
        now = datetime.datetime.utcnow()
        result = await testcases_model.update_many(
            {"_id": {"$in": object_ids}},
            {"$set": {"execution.lastExecutedAt": now, "updatedAt": now}},
        )
        return result
    except Exception as e:
        logger.error(f"Error updating testcase lastExecutedAt: {str(e)}")
        return None


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
