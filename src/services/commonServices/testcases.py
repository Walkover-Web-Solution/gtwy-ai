import pydash as _

from globals import logger
from src.configs.constant import bridge_ids
from src.services.utils.ai_call_util import call_ai_middleware
from src.services.utils.nlp import compute_cosine_similarity


async def compare_result(expected, actual, matching_type, response_type, ai_matching_custom_prompt=None):
    if response_type == "function":
        expected = {case["name"]: case.get("arguments", {}) for case in expected}
        actual = {case["name"]: case["args"] for case in actual or []}
    match matching_type:
        case "cosine":
            expected = str(expected)
            actual = str(actual)
            return compute_cosine_similarity(expected, actual)
        case "exact":
            return 1 if _.is_equal(expected, actual) else 0
        case "ai":
            variables = {"expected": str(expected)}
            if ai_matching_custom_prompt:
                variables["ai_matching_custom_prompt"] = ai_matching_custom_prompt
            response = await call_ai_middleware(
                str(actual), bridge_ids["compare_result"], variables=variables
            )
            return response["response"]["score"]


async def process_single_testcase_result(testcase_data, model_result, parsed_data):
    """
    Score a single testcase result. The score + metadata are returned so the
    caller can attach them to historyParams and let the log queue persist them
    onto the conversation_logs row in Postgres.
    """
    try:
        actual_result = (
            model_result.get("response", {}).get("data", {}).get("content", "")
            if isinstance(model_result, dict)
            else str(model_result)
        )

        expected_result = testcase_data.get("expected", {}).get(
            "response" if testcase_data.get("type") == "response" else "tool_calls", ""
        )

        matching_type = testcase_data.get("matching_type", "cosine")
        testcase_type = testcase_data.get("type", "response")

        ai_matching_custom_prompt = parsed_data.get("ai_matching_custom_prompt") if isinstance(parsed_data, dict) else None
        score = await compare_result(expected_result, actual_result, matching_type, testcase_type, ai_matching_custom_prompt)
        tools_call_data = (
            (model_result or {}).get("historyParams", {}).get("tools_call_data", [])
            if isinstance(model_result, dict) else []
        )

        return {
            "testcase_id": str(testcase_data.get("_id")),
            "expected": expected_result,
            "actual": actual_result,
            "score": score,
            "matching_type": matching_type,
            "type": testcase_type,
            "success": True,
            "tools_call_data": tools_call_data,
        }

    except Exception as e:
        logger.error(f"Error processing testcase result: {str(e)}")
        return {
            "testcase_id": str(testcase_data.get("_id")),
            "expected": testcase_data.get("expected", {}),
            "actual": None,
            "score": 0,
            "matching_type": testcase_data.get("matching_type", "cosine"),
            "error": str(e),
            "success": False,
        }
