import pydash as _
from src.db_services.testcase_services import create_testcases_history
from datetime import datetime
from src.services.utils.nlp import compute_cosine_similarity
from src.services.utils.ai_call_util import call_ai_middleware
from src.configs.constant import bridge_ids
from globals import logger


async def compare_result(expected, actual, matching_type, response_type):
    if(response_type == 'function'):
        expected = {case['name'] : case.get('arguments', {}) for case in expected}
        actual = {case['name'] : case['args'] for case in actual or []}
    match matching_type: 
        case 'cosine' : 
            expected = str(expected)
            actual = str(actual)
            return compute_cosine_similarity(expected, actual)
        case 'exact': 
                return 1 if _.is_equal(expected, actual) else 0
        case 'ai' : 
            response =  await call_ai_middleware(str(actual), bridge_ids['compare_result'], variables = ({'expected' : str(expected) }))
            return response['score']


async def process_single_testcase_result(testcase_data, model_result, parsed_data):
    """
    Process a single testcase result: calculate score and save to history
    """
    try:
        # Extract the actual response from model result
        actual_result = model_result.get('response', {}).get('data', {}).get('content', '') if isinstance(model_result, dict) else str(model_result)
        
        # Get expected result based on testcase type
        expected_result = testcase_data.get('expected', {}).get('response' if testcase_data.get('type') == 'response' else 'tool_calls', '')
        
        # Calculate score using the matching type
        matching_type = testcase_data.get('matching_type', 'cosine')
        testcase_type = testcase_data.get('type', 'response')
        
        score = await compare_result(expected_result, actual_result, matching_type, testcase_type)
        
        # Prepare data for saving to history
        data_to_insert = {
            'bridge_id': parsed_data.get('bridge_id'),
            'version_id': parsed_data.get('version_id'),
            'created_at': datetime.now().isoformat(),
            'testcase_id': str(testcase_data.get('_id')),
            'metadata': {
                'system_prompt': parsed_data.get('configuration', {}).get('prompt', ''),
                'model': parsed_data.get('configuration', {}).get('model', '')
            },
            'model_output': actual_result,
            'score': score
        }
        
        # Save to testcase history
        await create_testcases_history([data_to_insert])
        
        return {
            'testcase_id': str(testcase_data.get('_id')),
            'expected': expected_result,
            'actual': actual_result,
            'score': score,
            'matching_type': matching_type,
            'success': True
        }
        
    except Exception as e:
        logger.error(f"Error processing testcase result: {str(e)}")
        return {
            'testcase_id': str(testcase_data.get('_id')),
            'expected': testcase_data.get('expected', {}),
            'actual': None,
            'score': 0,
            'matching_type': testcase_data.get('matching_type', 'cosine'),
            'error': str(e),
            'success': False
        }
