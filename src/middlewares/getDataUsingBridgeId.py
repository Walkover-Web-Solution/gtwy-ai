from fastapi import Request, HTTPException
from src.services.utils.getConfiguration import getConfiguration
from globals import *
from src.configs.model_configuration import model_config_document

async def add_configuration_data_to_body(request: Request):

    try:
        body = await request.json()
        org_id = request.state.profile['org']['id']
        chatbotData = getattr(request.state, "chatbot", None)
        if chatbotData:
            body.update(chatbotData)
        # Handle bridge configuration
        bridge_id = body.get('agent_id') or body.get("bridge_id") or request.path_params.get('bridge_id') or getattr(request.state, 'chatbot', {}).get('bridge_id', None)
        if chatbotData:
            del request.state.chatbot
        version_id = body.get('version_id') or request.path_params.get('version_id')
        db_config = await getConfiguration(
            body.get('configuration'), 
            body.get('service'), 
            bridge_id, 
            body.get('apikey'), 
            body.get('template_id'), 
            body.get('variables', {}), 
            org_id, 
            body.get('variables_path'), 
            version_id=version_id, 
            extra_tools=body.get('extra_tools', []), 
            built_in_tools=body.get('built_in_tools'),
            guardrails=body.get('guardrails'),
            web_search_filters=body.get('web_search_filters'),
            orchestrator_flag = body.get('orchestrator_flag'),
            chatbot=body.get('chatbot', False)
        )
        
        # Check if getConfiguration returned an error response
        if not db_config.get('success', True) or db_config.get('error'):
            # Return the actual error from getConfiguration directly
            raise HTTPException(status_code=400, detail=db_config)
        
        bridge_configurations = db_config.get('bridge_configurations') or {}

        if not bridge_configurations:
            raise HTTPException(status_code=400, detail={"success": False, "error": "Unable to resolve bridge configuration"})

        target_bridge_id = bridge_id or db_config.get('primary_bridge_id')
        if target_bridge_id and target_bridge_id in bridge_configurations:
            primary_config = bridge_configurations[target_bridge_id]
        else:
            primary_config = next(iter(bridge_configurations.values()))
        if not isinstance(primary_config.get("images"), list) and not isinstance(body.get("images"), list):
            primary_config["images"] = []
        if not isinstance(primary_config.get("files"), list) and not isinstance(body.get("files"), list):
            primary_config["files"] = []
        body.update(primary_config)
        body['bridge_configurations'] = bridge_configurations
        service = body.get("service")
        model = body.get("configuration").get('model')
        user = body.get("user")
        images = body.get("images") or []
        batch = body.get("batch") or []
        if user is None and len(images) == 0 and len(batch) == 0:
            raise HTTPException(status_code=400, detail={"success": False, "error": "User message is compulsory"})
        if not (service in model_config_document and model in model_config_document[service]):
            raise HTTPException(status_code=400, detail={"success": False, "error": "model or service does not exist!"})
        if model_config_document[service][model].get('org_id'):
            if model_config_document[service][model]['org_id'] != org_id:
                raise HTTPException(status_code=400, detail={"success": False, "error": "model or service does not exist!"})
            
        return db_config
    except HTTPException as he:
         raise he
    except Exception as e:
        
        logger.error(f"Error in get_data: {str(e)}, {traceback.format_exc()}")
        raise HTTPException(status_code=400, detail={"success": False, "error": "Error in getting data: "+ str(e)})
    

