from ...db_services.webhook_alert_Dbservice import get_webhook_data
from ..commonServices.baseService.baseService import sendResponse
from .helper import Helper
from src.services.proxy.Proxyservice import get_user_org_mapping
from globals import *

async def send_error_to_webhook(bridge_id, org_id, error_log, error_type, bridge_name=None, is_embed=None, user_id=None,thread_id=None, service=None):
    """
    Sends error logs to a webhook if the specified conditions are met.

    Args:
        bridge_id (str): Identifier for the bridge.
        org_id (str): Identifier for the organization.
        error_log (dict): Error log details.
        error_type (str): Type of the error (e.g., 'Variable', 'Error', 'metrix_limit_reached').

    Returns:
        None
    """
    try:
        # Fetch webhook data for the organization
        result = await get_webhook_data(org_id)
        if not result or 'webhook_data' not in result:
            raise BadRequestException("Webhook data is missing in the response.")

        webhook_data = result['webhook_data']

        # Add default alert configuration if necessary
        webhook_data.append({
            "org_id": org_id,
            "name": "default alert",
            "webhookConfiguration": {
                "url": "https://flow.sokt.io/func/scriSmH2QaBH",
                "headers": {}
            },
            "alertType": ["Error", "Variable", "retry_mechanism"],
            "bridges": ["all"]
        })

        # Generate the appropriate payload based on the error type
        
        if error_type == 'Variable': 
            details_payload = create_missing_vars(error_log)
        elif error_type == 'metrix_limit_reached':
            details_payload = metrix_limit_reached(error_log)
        elif error_type == 'retry_mechanism':
            details_payload = create_retry_mechanism_payload(error_log)
        else:
            details_payload = create_error_payload(error_log)
        

        # Iterate through webhook configurations and send responses
        for entry in webhook_data:
            webhook_config = entry.get('webhookConfiguration')
            bridges = entry.get('bridges', [])

            if error_type in entry.get('alertType', []) and (bridge_id in bridges or 'all' in bridges):
                if(error_type == 'metrix_limit_reached' and entry.get('limit', 500) == error_log): 
                    continue
                webhook_url = webhook_config['url']
                headers = webhook_config.get('headers', {})

                # Prepare details for the webhook
                payload = {
                    "details": details_payload,  # Use details_payload directly to avoid nesting
                    "bridge_id": bridge_id,
                    "org_id": org_id,
                    "user_id": user_id,
                    "thread_id": thread_id,        
                    "service": service,

                }
                
                # Add bridge_name and is_embed to payload if available
                if bridge_name is not None:
                    payload["bridge_name"] = bridge_name
                if is_embed is not None:
                    payload["is_embed"] = is_embed

                # Fetch user org mapping only if user_id is available
                if user_id and is_embed:
                    userinfo = await get_user_org_mapping(user_id, org_id)
                    embed_user_id = Helper.extract_embed_user_id(userinfo, org_id)
                    if embed_user_id:
                        payload["embeduserId"] = embed_user_id

                # Send the response
                response_format = create_response_format(webhook_url, headers)
                await sendResponse(response_format, data=payload)

    except Exception as error:
        logger.error(f'Error in send_error_to_webhook: %s, {str(error)}')

def create_missing_vars(details):
    return {
        "alert" : "variables missing",
        "Variables" : details
    }

def metrix_limit_reached(details):
    return {
        "alert" : "limit_reached",
        "Limit Size" : details
    }

def create_error_payload(details):
    return {
        "alert" : "Unexpected Error",
        "error_message" : details['error']
    }

def create_retry_mechanism_payload(details):
    return {
        "alert" : "Retry Mechanism Started due to error.",
        "error_message" : details
    }

def create_response_format(url, headers):
    return {
        "type": "webhook",
        "cred": {
            "url": url,
            "headers": headers
        }
    }
