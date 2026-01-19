from models.mongo_connection import db
from bson import ObjectId
from ..services.cache_service import find_in_cache, store_in_cache, delete_in_cache, make_json_serializable
import json
from globals import *
from bson import errors
from src.configs.constant import redis_keys
from config import Config
import jwt
from datetime import datetime
from ..services.utils.apiservice import fetch

configurationModel = db["configurations"]
apiCallModel = db['apicalls']
templateModel = db['templates']
apikeyCredentialsModel = db['apikeycredentials']
version_model = db['configuration_versions']
threadsModel = db['threads']
foldersModel = db['folders']

async def get_bridges_with_redis(bridge_id = None, org_id = None, version_id = None):
    try:
        cache_key = f"{redis_keys['get_bridge_data_']}{version_id or bridge_id}"
        cached_data = await find_in_cache(cache_key)
        if cached_data:
            cached_result = json.loads(cached_data)
            return cached_result[0] if cached_result else {}
        model = version_model if version_id else configurationModel
        id_to_use = ObjectId(version_id) if version_id else ObjectId(bridge_id)
        pipeline = [
            {
                '$match': {'_id': ObjectId(id_to_use), 'org_id': org_id}
            },
            {
                '$project': {
                    'configuration.encoded_prompt': 0
                }
            },
            {
                '$addFields': {
                    '_id': {'$toString': '$_id'},
                    'function_ids': {
                        '$map': {
                            'input': '$function_ids',
                            'as': 'fid',
                            'in': {'$toString': '$$fid'}
                        }
                    }
                }
            }
        ]
        
        result = await model.aggregate(pipeline).to_list(length=None)
        bridges = result[0] if result else {}
        await store_in_cache(cache_key, result)
        return {
            'success': True,
            'bridges': bridges,
        }
    except Exception as error:
        logger.error(f'Error in get bridges : {str(error)}')
        return {
            'success': False,
            'error': "something went wrong!!"
        }
# todo
async def get_bridges_without_tools(bridge_id=None, org_id=None, version_id=None):
    try:
        model = version_model if version_id else configurationModel
        id_to_use = ObjectId(version_id) if version_id else ObjectId(bridge_id)
        bridge_data = await model.find_one({'_id': ObjectId(id_to_use)})

        if not bridge_data:
            raise errors.InvalidId("No matching bridge found")

        return {
            'success': True,
            'bridges': bridge_data,
        }
    except errors.InvalidId:
        raise BadRequestException("Invalid Bridge ID provided")
    except Exception as error:
        logger.error(f'Error in get_bridges_without_tools : {str(error)}')
        raise error

async def get_bridges_with_tools_and_apikeys(bridge_id, org_id, version_id=None):
    try:
        cache_key = f"{redis_keys['bridge_data_with_tools_']}{version_id or bridge_id}"
       
        # Attempt to retrieve data from Redis cache
        cached_data = await find_in_cache(cache_key)
        if cached_data:
            return json.loads(cached_data)

        model = version_model if version_id else configurationModel
        id_to_use = ObjectId(version_id) if version_id else ObjectId(bridge_id)
        pipeline = [
    # Stage 0: Match the specific bridge or version with the given org_id
    {
        '$match': {'_id': ObjectId(id_to_use), "org_id": org_id}
    },
    {
        '$project': {
            'configuration.encoded_prompt': 0
        }
    },
    # Stage 1: Lookup to join with 'apicalls' collection
    {
        '$lookup': {
            'from': 'apicalls',
            'localField': 'function_ids',
            'foreignField': '_id',
            'as': 'apiCalls'
        }
    },
    # Stage 2: Restructure fields for _id, function_ids and apiCalls
    {
        '$addFields': {
            '_id': {'$toString': '$_id'},
            'function_ids': {
                '$map': {
                    'input': '$function_ids',
                    'as': 'fid',
                    'in': {'$toString': '$$fid'}
                }
            },
            'apiCalls': {
                '$arrayToObject': {
                    '$map': {
                        'input': '$apiCalls',
                        'as': 'api_call',
                        'in': {
                            'k': {'$toString': '$$api_call._id'},
                            'v': {
                                '$mergeObjects': [
                                    '$$api_call',
                                    {
                                        '_id': {'$toString': '$$api_call._id'},
                                        'bridge_ids': {
                                            '$map': {
                                                'input': '$$api_call.bridge_ids',
                                                'as': 'bid',
                                                'in': {'$toString': '$$bid'}
                                            }
                                        }
                                    }
                                ]
                            }
                        }
                    }
                }
            }
        }
    },
    # Stage 3: Convert 'apikey_object_id' to an array of key-value pairs, handling null case
    {
        '$addFields': {
            'apikey_object_id_safe': { '$ifNull': ['$apikey_object_id', {}] },
            'has_apikeys': { '$cond': [{ '$eq': [{ '$type': '$apikey_object_id' }, 'object'] }, True, False] }
        }
    },
    {
        '$addFields': {
            'apikeys_array': { '$cond': [
                '$has_apikeys',
                { '$objectToArray': '$apikey_object_id_safe' },
                []
            ]}
        }
    },
    # Stage 4: Lookup 'apikeycredentials' using the ObjectIds from 'apikeys_array.v'
    {
        '$lookup': {
            'from': 'apikeycredentials',
            'let': {
                'apikey_ids_object': {
                    '$cond': [
                        { '$gt': [{ '$size': '$apikeys_array' }, 0] },
                        {
                            '$map': {
                                'input': '$apikeys_array.v',
                                'as': 'id',
                                'in': {
                                    '$convert': {
                                        'input': '$$id',
                                        'to': 'objectId',
                                        'onError': None,
                                        'onNull': None
                                    }
                                }
                            }
                        },
                        []
                    ]
                }
            },
            'pipeline': [
                {
                    '$match': {
                        '$expr': {
                            '$cond': [
                                { '$gt': [{ '$size': '$$apikey_ids_object' }, 0] },
                                { '$in': ['$_id', '$$apikey_ids_object'] },
                                False
                            ]
                        }
                    }
                },
                {
                    '$project': { 'service': 1, 'apikey': 1, 'apikey_limit': { '$ifNull': ['$apikey_limit', 0] },
                     'apikey_usage': { '$ifNull': ['$apikey_usage', 0] } }
                }
            ],
            'as': 'apikeys_docs'
        }
    },
    # Stage 5: Map each service to its corresponding apikey, handling empty case
    {
        '$addFields': {
            'apikeys': {
                '$cond': [
                    { '$gt': [{ '$size': '$apikeys_array' }, 0] },
                    {
                        '$arrayToObject': {
                            '$map': {
                                'input': '$apikeys_array',
                                'as': 'item',
                                'in': [
                                    '$$item.k',  # Service name as the key
                                    {
                                        '$arrayElemAt': [
                                            {
                                                '$map': {
                                                    'input': {
                                                        '$filter': {
                                                            'input': '$apikeys_docs',
                                                            'as': 'doc',
                                                            'cond': {
                                                                '$eq': [
                                                                    '$$doc._id',
                                                                    {
                                                                        '$convert': {
                                                                            'input': '$$item.v',
                                                                            'to': 'objectId',
                                                                            'onError': None,
                                                                            'onNull': None
                                                                        }
                                                                    }
                                                                ]
                                                            }
                                                        }
                                                    },
                                                    'as': 'matched_doc',
                                                    'in': {
                                                    'apikey': '$$matched_doc.apikey',
                                                    'apikey_limit': '$$matched_doc.apikey_limit', 
                                                    'apikey_usage': '$$matched_doc.apikey_usage'
                                                    }       
                                                }
                                            },
                                            0  # Get the first matched apikey
                                        ]
                                    }
                                ]
                            }
                        }
                    },
                    {}
                ]
            }
        }
    },
    # Stage 6: Lookup 'pre_tools' data from 'apicalls' collection using the ObjectIds in 'pre_tools'
    {
        '$lookup': {
            'from': 'apicalls',
            'let': {
                'pre_tools_ids': {
                    '$map': {
                        'input': '$pre_tools',
                        'as': 'id',
                        'in': {
                            '$convert': {
                                'input': '$$id',
                                'to': 'objectId',
                                'onError': None,
                                'onNull': None
                            }
                        }
                    }
                }
            },
            'pipeline': [
                {
                    '$match': {
                        '$expr': {
                            '$in': ['$_id', {'$ifNull': ['$$pre_tools_ids', []]}]
                        }
                    }
                }
            ],
            'as': 'pre_tools_data'
        }
    },
    # Stage 7: Extract bridge_ids from connected_agents if it exists
    {
        '$addFields': {
            'connected_agents_bridge_ids': {
                '$cond': [
                    { '$and': [
                        { '$ne': ['$connected_agents', None] },
                        { '$ne': ['$connected_agents', {}] },
                        { '$eq': [{ '$type': '$connected_agents' }, 'object'] }
                    ]},
                    {
                        '$map': {
                            'input': { '$objectToArray': '$connected_agents' },
                            'as': 'agent',
                            'in': {
                                '$convert': {
                                    'input': '$$agent.v.bridge_id',
                                    'to': 'objectId',
                                    'onError': None,
                                    'onNull': None
                                }
                            }
                        }
                    },
                    []
                ]
            }
        }
    },
    # Stage 8: Lookup connected_agent_details from configurations collection
    {
        '$lookup': {
            'from': 'configurations',
            'let': {
                'bridge_ids': {
                    '$filter': {
                        'input': '$connected_agents_bridge_ids',
                        'as': 'id',
                        'cond': { '$ne': ['$$id', None] }
                    }
                }
            },
            'pipeline': [
                {
                    '$match': {
                        '$expr': {
                            '$and': [
                                { '$in': ['$_id', '$$bridge_ids'] },
                                { '$ne': ['$connected_agent_details', None] },
                                { '$ne': ['$connected_agent_details', {}] }
                            ]
                        }
                    }
                },
                {
                    '$project': {
                        '_id': 1,
                        'connected_agent_details': 1
                    }
                },
                {
                    '$addFields': {
                        '_id': { '$toString': '$_id' }
                    }
                }
            ],
            'as': 'agent_details_docs'
        }
    },
    # Stage 9: Create connected_agent_details object with bridge_id as key
    {
        '$addFields': {
            'connected_agent_details': {
                '$cond': [
                    { '$gt': [{ '$size': '$agent_details_docs' }, 0] },
                    {
                        '$arrayToObject': {
                            '$map': {
                                'input': '$agent_details_docs',
                                'as': 'doc',
                                'in': [
                                    '$$doc._id',
                                    '$$doc.connected_agent_details'
                                ]
                            }
                        }
                    },
                    {}
                ]
            }
        }
    },
    # Stage 10: Remove temporary fields to clean up the output
    {
        '$project': {
            'apikeys_array': 0,
            'apikeys_docs': 0,
            'apikey_object_id_safe': 0,
            'has_apikeys': 0,
            'connected_agents_bridge_ids': 0,
            'agent_details_docs': 0
            # Exclude additional temporary fields as needed
        }
    }
]
       
        # Execute the main aggregation pipeline
        result = await model.aggregate(pipeline).to_list(length=None)
       
        if not result:
            return {
                'success': False,
                'error': 'No matching records found'
            }
        
        bridge_data = result[0]
        
        # Check if folder_id is present and fetch folder API keys
        if bridge_data.get('folder_id'):
            
            folder_pipeline = [
                # Stage 1: Match the folder document
                {
                    '$match': {'_id': ObjectId(bridge_data['folder_id'])}
                },
                # Stage 2: Convert apikey_object_id to array format
                {
                    '$addFields': {
                        'apikey_object_id_safe': { '$ifNull': ['$apikey_object_id', {}] },
                        'has_apikeys': { '$cond': [{ '$eq': [{ '$type': '$apikey_object_id' }, 'object'] }, True, False] },
                        'folder_limit': { '$ifNull': ['$folder_limit', 0] },
                        'folder_usage': { '$ifNull': ['$folder_usage', 0] }
                    }
                },
                {
                    '$addFields': {
                        'apikeys_array': { '$cond': [
                            '$has_apikeys',
                            { '$objectToArray': '$apikey_object_id_safe' },
                            []
                        ]}
                    }
                },
                # Stage 3: Lookup apikeycredentials
                {
                    '$lookup': {
                        'from': 'apikeycredentials',
                        'let': {
                            'apikey_ids_object': {
                                '$cond': [
                                    { '$gt': [{ '$size': '$apikeys_array' }, 0] },
                                    {
                                        '$map': {
                                            'input': '$apikeys_array.v',
                                            'as': 'id',
                                            'in': {
                                                '$convert': {
                                                    'input': '$$id',
                                                    'to': 'objectId',
                                                    'onError': None,
                                                    'onNull': None
                                                }
                                            }
                                        }
                                    },
                                    []
                                ]
                            }
                        },
                        'pipeline': [
                            {
                                '$match': {
                                    '$expr': {
                                        '$in': ['$_id', { '$ifNull': ['$$apikey_ids_object', []] }]
                                    }
                                }
                            }
                        ],
                        'as': 'apikeys_docs'
                    }
                },
                # Stage 4: Create folder_apikeys object with apikey, limit, usage
                {
                    '$addFields': {
                        'folder_apikeys': {
                            '$cond': [
                                { '$gt': [{ '$size': '$apikeys_array' }, 0] },
                                {
                                    '$arrayToObject': {
                                        '$map': {
                                            'input': '$apikeys_array',
                                            'as': 'item',
                                            'in': {
                                                'k': '$$item.k',
                                                'v': {
                                                    '$let': {
                                                        'vars': {
                                                            'matched': {
                                                                '$arrayElemAt': [
                                                                    {
                                                                        '$filter': {
                                                                            'input': '$apikeys_docs',
                                                                            'as': 'doc',
                                                                            'cond': {
                                                                                '$eq': [
                                                                                    '$$doc._id',
                                                                                    {
                                                                                        '$convert': {
                                                                                            'input': '$$item.v',
                                                                                            'to': 'objectId',
                                                                                            'onError': None,
                                                                                            'onNull': None
                                                                                        }
                                                                                    }
                                                                                ]
                                                                            }
                                                                        }
                                                                    },
                                                                    0
                                                                ]
                                                            }
                                                        },
                                                        'in': {
                                                            'apikey': '$$matched.apikey',
                                                            'apikey_limit': { '$ifNull': ['$$matched.apikey_limit', 0] },
                                                            'apikey_usage': { '$ifNull': ['$$matched.apikey_usage', 0] }
                                                        }
                                                    }
                                                }
                                            }
                                        }
                                    }
                                },
                                {}
                            ]
                        }
                    }
                },
                # Stage 5: Project folder_apikeys and type
                {
                    '$project': {
                        'folder_apikeys': 1,
                        'type': 1,
                        'folder_limit': { '$ifNull': ['$folder_limit', 0] },
                        'folder_usage': { '$ifNull': ['$folder_usage', 0] },
                        'apikey_object_id': 1,
                    }
                }
            ]

            # Execute folder pipeline on folders collection
            folder_result = await foldersModel.aggregate(folder_pipeline).to_list(length=None)
            
            # Append folder_apikeys to bridge_data if found
            if folder_result and folder_result[0].get('folder_apikeys'):
                bridge_data['folder_apikeys'] = folder_result[0]['folder_apikeys']
                bridge_data['apikey_object_id'] = folder_result[0]['apikey_object_id']
            else:
                bridge_data['folder_apikeys'] = {}

            if folder_result and folder_result[0].get('type'):
                bridge_data['folder_type'] = folder_result[0]['type']
            else:
                bridge_data['folder_type'] = None

            if folder_result and folder_result[0].get('folder_limit'):
                bridge_data['folder_limit'] = folder_result[0]['folder_limit']
            else:
                bridge_data['folder_limit'] = 0
                
            if folder_result and folder_result[0].get('folder_usage'):
                bridge_data['folder_usage'] = folder_result[0]['folder_usage']
            else :
                bridge_data['folder_usage'] = 0
            
        else:
            # No folder_id, set empty folder_apikeys and folder_type
            bridge_data['folder_apikeys'] = {}
            bridge_data['folder_limit'] = 0
            bridge_data['folder_usage'] = 0
            bridge_data['folder_type'] = None
       
        # Structure the final response
        response = {
            'success': True,
            'bridges': bridge_data
        }
        await store_in_cache(cache_key, response)
        return response

    except errors.InvalidId:
        raise BadRequestException("Invalid Bridge ID provided")
    except Exception as error:
        logger.error(f'Error in get_bridges_with_tools_and_apikeys: {str(error)}')
        raise error

async def get_template_by_id(template_id):
    try:
        cache_key = f"template_{template_id}"
        template_content = await find_in_cache(cache_key)
        if template_content:
            template_content = json.loads(template_content)
            return template_content
        
        template_content = await templateModel.find_one({'_id' : ObjectId(template_id)})
        await store_in_cache(cache_key, template_content)
        return template_content
    except Exception as error : 
        logger.error(f"Error in get_template_by_id: {str(error)}")
        return None
    
async def get_bridge_by_slugname(org_id, slug_name):
    try:
        bridge =  await configurationModel.find_one({
            'slugName': slug_name,
            'org_id': org_id
        })

        if not bridge:
            raise BadRequestException("Bridge not found")

        if 'responseRef' in bridge:
            response_ref_id = bridge['responseRef']
            response_ref = await configurationModel.find_one({'_id': response_ref_id})
            bridge['responseRef'] = response_ref

        return bridge
    except BadRequestException:
        raise
    except Exception as error:
        logger.error(f'Error in get_bridge_by_slugname: {str(error)}')
        raise BadRequestException("Failed to fetch bridge by slugName")


async def update_bridge(bridge_id = None, update_fields = None, version_id = None):
    model = version_model if version_id else configurationModel
    id_to_use = ObjectId(version_id) if version_id else ObjectId(bridge_id)
    cache_key = f"{version_id if version_id else bridge_id}"
    
    updated_bridge = await model.find_one_and_update(
        {'_id': ObjectId(id_to_use)},
        {'$set': update_fields},
        return_document=True,
        upsert=True
    )

    if not updated_bridge:
        raise BadRequestException('Bridge not found or no records updated')

    updated_bridge['_id'] = str(updated_bridge['_id'])  # Convert ObjectId to string
    if 'function_ids' in updated_bridge and updated_bridge['function_ids'] is not None:
        updated_bridge['function_ids'] = [str(fid) for fid in updated_bridge['function_ids']]  # Convert function_ids to string

    await delete_in_cache(f"{redis_keys['bridge_data_with_tools_']}{cache_key}")
    return {
        'success': True,
        'result': updated_bridge
    }

async def save_sub_thread_id(org_id, thread_id, sub_thread_id, display_name, bridge_id,current_time): # bridge_id is now a required parameter
    try:
        
        
        # Build update data with both $set and $setOnInsert in single operation
        update_data = {
            '$set': {
                'bridge_id': bridge_id
            },
            '$setOnInsert': {
                'org_id': org_id,
                'thread_id': thread_id,
                'sub_thread_id': sub_thread_id,
                'created_at': current_time
            }
        }
        
        # Add display_name to $set if provided
        if display_name is not None and isinstance(display_name, str):
            update_data['$set']['display_name'] = display_name
       
        result = await threadsModel.find_one_and_update(
            {'org_id': org_id, 'thread_id': thread_id, 'sub_thread_id': sub_thread_id, 'bridge_id': bridge_id},
            update_data,
            upsert=True,
            return_document=True
        )
        return {
            'success': True,
            'message': f"sub_thread_id and bridge_id saved successfully {result}" # Updated success message
        }
    except Exception as error:
        logger.error(f"Error in save_sub_thread_id: {error}")
        raise error    

async def get_agents_data(slug_name, user_email):
    bridges = await configurationModel.find_one({
        "$or": [
            {
                "$and": [
                    {"page_config.availability": "public"},
                    {"page_config.url_slugname": slug_name}
                ]
            },
            {
                "$and": [
                    {"page_config.availability": "private"},
                    {"page_config.url_slugname": slug_name},
                    {"page_config.allowedUsers": user_email}
                ]
            }
        ]
    })
    return bridges
