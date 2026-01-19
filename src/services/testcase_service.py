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
from typing import List, Dict, Any, Optional
from bson import ObjectId

from models.mongo_connection import db
from src.services.utils.getConfiguration import getConfiguration
from src.services.commonServices.common import chat

logger = logging.getLogger(__name__)


class TestcaseValidationError(Exception):
    """Custom exception for testcase validation errors"""
    pass


class TestcaseNotFoundError(Exception):
    """Custom exception for testcase not found errors"""
    pass


def validate_testcase_request_data(body: Dict[str, Any]) -> Dict[str, Any]:
    """
    Validate and extract testcase request data
    
    Args:
        body: Request body dictionary
        
    Returns:
        Dictionary containing validated request parameters
        
    Raises:
        TestcaseValidationError: If required fields are missing or invalid
    """
    version_id = body.get('version_id')
    if not version_id:
        raise TestcaseValidationError("version_id is required")
    
    bridge_id = body.get('bridge_id')
    testcase_id = body.get('testcase_id')
    testcases_flag = body.get('testcases', False)
    testcase_data = body.get('testcase_data')
    
    return {
        'bridge_id': bridge_id,
        'version_id': version_id,
        'testcase_id': testcase_id,
        'testcases_flag': testcases_flag,
        'testcase_data': testcase_data
    }


def validate_direct_testcase_data(testcase_data: Dict[str, Any]) -> None:
    """
    Validate direct testcase data fields
    
    Args:
        testcase_data: Dictionary containing testcase data
        
    Raises:
        TestcaseValidationError: If required fields are missing
    """
    required_fields = ['conversation', 'expected', 'matching_type']
    
    for field in required_fields:
        if field not in testcase_data:
            raise TestcaseValidationError(f"{field} is required in testcase_data")


async def fetch_testcases_from_request(
    testcases_flag: bool,
    testcase_data: Optional[Dict[str, Any]],
    bridge_id: Optional[str],
    testcase_id: Optional[str]
) -> List[Dict[str, Any]]:
    """
    Fetch testcases either from direct input or MongoDB
    
    Args:
        testcases_flag: Flag indicating if testcase data is provided directly
        testcase_data: Direct testcase data (if provided)
        bridge_id: Bridge ID for MongoDB query
        testcase_id: Specific testcase ID for MongoDB query
        
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
            '_id': 'direct_testcase',
            'bridge_id': bridge_id,
            'conversation': testcase_data.get('conversation', []),
            'expected': testcase_data.get('expected', {}),
            'matching_type': testcase_data.get('matching_type', 'cosine'),
            'type': 'response'
        }
        
        return [testcase]
    
    else:
        # Fetch from MongoDB
        if not bridge_id:
            raise TestcaseValidationError("bridge_id is required")
        
        testcases_collection = db['testcases']
        
        if testcase_id:
            # Fetch specific testcase by ID
            testcase = await testcases_collection.find_one({"_id": ObjectId(testcase_id)})
            if not testcase:
                raise TestcaseNotFoundError("No testcase found for the given testcase_id")
            return [testcase]
        else:
            # Fetch all testcases for bridge_id
            testcases = await testcases_collection.find({"bridge_id": bridge_id}).to_list(length=None)
            if not testcases:
                raise TestcaseNotFoundError("No testcases found for the given bridge_id")
            return testcases


async def get_testcase_configuration(
    org_id: str,
    version_id: str,
    bridge_id: Optional[str],
    testcases_flag: bool,
    testcase_data: Optional[Dict[str, Any]]
) -> Dict[str, Any]:
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
        {},
        org_id,
        None,
        version_id=version_id,
        extra_tools=[],
        built_in_tools=None,
        guardrails=None
    )
    
    if not db_config.get("success"):
        raise TestcaseValidationError(db_config.get("error", "Failed to get configuration"))
    
    primary_bridge_id = db_config.get('primary_bridge_id')
    bridge_configurations = db_config.get('bridge_configurations', {})

    return bridge_configurations[primary_bridge_id]

async def process_single_testcase(testcase: Dict[str, Any], db_config: Dict[str, Any]) -> Dict[str, Any]:
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
        if 'configuration' in db_config:
            db_config['configuration'] = db_config['configuration'].copy()

        # Set conversation in db_config
        db_config['configuration']['conversation'] = testcase.get('conversation', [])
        
        # Create request data for this testcase
        testcase_request_data = {
            "body": {
                "user": testcase.get('conversation', [])[-1].get('content', '') if testcase.get('conversation') else '',
                "testcase_data": {
                    "matching_type": testcase.get('matching_type') or 'cosine',
                    "run_testcase": True,
                    "_id": testcase.get('_id'),
                    "expected": testcase.get('expected'),
                    "type": testcase.get('type', 'response')
                },
                **db_config
            },
            "state": {
                "is_playground": True,
                "version": 2
            }
        }
        
        # Call chat function
        result = await chat(testcase_request_data)
        
        # Extract data from JSONResponse object
        if hasattr(result, 'body'):
            result_data = json.loads(result.body.decode('utf-8'))
        else:
            result_data = result
        
        # Extract testcase result with score if available
        testcase_result = result_data.get('response', {}).get('testcase_result', {}) if isinstance(result_data, dict) else {}
        
        return {
            "testcase_id": str(testcase.get('_id')) if testcase.get('_id') != 'direct_testcase' else 'direct_testcase',
            "bridge_id": testcase.get('bridge_id'),
            "expected": testcase.get('expected'),
            "actual_result": result_data.get('response', {}).get('data', {}).get('content', '') if isinstance(result_data, dict) else str(result_data),
            "score": testcase_result.get('score'),
            "matching_type": testcase.get('matching_type', ''),
            "success": True
        }
        
    except Exception as e:
        logger.error(f"Error processing testcase {testcase.get('_id')}: {str(e)}")
        return {
            "testcase_id": str(testcase.get('_id')) if testcase.get('_id') != 'direct_testcase' else 'direct_testcase',
            "bridge_id": testcase.get('bridge_id'),
            "expected": testcase.get('expected'),
            "actual_result": None,
            "score": 0,
            "matching_type": testcase.get('matching_type', 'cosine'),
            "error": str(e),
            "success": False
        }


async def run_testcases_parallel(testcases: List[Dict[str, Any]], db_config: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    Run multiple testcases in parallel
    
    Args:
        testcases: List of testcase dictionaries
        db_config: Configuration dictionary
        
    Returns:
        List of testcase results
    """
    # Process all testcases in parallel
    results = await asyncio.gather(*[
        process_single_testcase(testcase, db_config.copy()) 
        for testcase in testcases
    ])
    
    return results


async def execute_testcases(
    body: Dict[str, Any],
    org_id: str
) -> Dict[str, Any]:
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
    # Validate request data
    request_data = validate_testcase_request_data(body)
    
    # Fetch testcases
    testcases = await fetch_testcases_from_request(
        request_data['testcases_flag'],
        request_data['testcase_data'],
        request_data['bridge_id'],
        request_data['testcase_id']
    )
    
    # Get configuration
    db_config = await get_testcase_configuration(
        org_id,
        request_data['version_id'],
        request_data['bridge_id'],
        request_data['testcases_flag'],
        request_data['testcase_data']
    )
    
    # Run testcases in parallel
    results = await run_testcases_parallel(testcases, db_config)
    
    # Return formatted response
    return {
        "success": True,
        "bridge_id": request_data['bridge_id'],
        "version_id": request_data['version_id'],
        "total_testcases": len(testcases),
        "results": results,
        "testcase_source": "direct" if (request_data['testcases_flag'] and request_data['testcase_data']) else "mongodb"
    }
