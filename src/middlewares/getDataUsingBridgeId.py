from typing import Type

from fastapi import HTTPException, Request
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, ValidationError

from globals import logger, traceback
from src.configs.model_configuration import model_config_document
from src.services.billing.billing_utils import release_credits, reserve_credits_and_api_key_setup
from src.services.utils.getConfiguration import getConfiguration


class add_configuration_data_to_body:
    """FastAPI dependency that optionally validates the request body against a
    Pydantic schema before running the bridge-configuration logic.

    Usage:
        Depends(add_configuration_data_to_body(CompletionRequest))   # validates
        await add_configuration_data_to_body(schema_class=None)(request)  # skip validation
    """

    def __init__(self, schema_class: Type[BaseModel] | None = None):
        self.schema_class = schema_class

    async def __call__(self, request: Request):
        credit_hold_token = None
        org_id = None
        try:
            body = await request.json()

            if self.schema_class is not None:
                try:
                    self.schema_class.model_validate(body)
                except ValidationError as exc:
                    raise RequestValidationError(errors=exc.errors()) from exc

            org_id = request.state.profile["org"]["id"]
            chatbotData = getattr(request.state, "chatbot", None)
            if chatbotData:
                body.update(chatbotData)
            # Handle bridge configuration
            bridge_id = (
                body.get("agent_id")
                or body.get("bridge_id")
                or request.path_params.get("bridge_id")
                or getattr(request.state, "chatbot", {}).get("bridge_id", None)
            )
            if chatbotData:
                del request.state.chatbot
            version_id = body.get("version_id") or request.path_params.get("version_id")
            db_config = await getConfiguration(
                body.get("configuration"),
                body.get("service"),
                bridge_id,
                body.get("apikey"),
                body.get("template_id"),
                body.get("variables", {}),
                org_id,
                body.get("variables_path"),
                version_id=version_id,
                extra_tools=body.get("extra_tools", []),
                built_in_tools=body.get("built_in_tools"),
                guardrails=body.get("guardrails"),
                web_search_filters=body.get("web_search_filters"),
                orchestrator_flag=body.get("orchestrator_flag"),
                chatbot=body.get("chatbot", False),
                override_fields=body,
                environment=body.get("environment"),
            )

            # Check if getConfiguration returned an error response
            if not db_config.get("success", True) or db_config.get("error"):
                # Return the actual error from getConfiguration directly
                raise HTTPException(status_code=400, detail=db_config)

            credit_hold_token, credit_error = await reserve_credits_and_api_key_setup(
                org_id, db_config, is_batch=bool(body.get("batch"))
            )
            if credit_error:
                raise HTTPException(status_code=400, detail=credit_error)

            bridge_configurations = db_config.get("bridge_configurations") or {}

            if not bridge_configurations:
                raise HTTPException(
                    status_code=400, detail={"success": False, "error": "Unable to resolve bridge configuration"}
                )

            target_bridge_id = bridge_id or db_config.get("primary_bridge_id")
            if target_bridge_id and target_bridge_id in bridge_configurations:
                primary_config = bridge_configurations[target_bridge_id]
            else:
                primary_config = next(iter(bridge_configurations.values()))
            if not isinstance(primary_config.get("images"), list) and not isinstance(body.get("images"), list):
                primary_config["images"] = []
            if not isinstance(primary_config.get("files"), list) and not isinstance(body.get("files"), list):
                primary_config["files"] = []

            # When request comes via modelRouter (jwt_middleware only), profile may not have owner_id.
            # Derive it from bridge config using same logic as interface middleware.
            if not request.state.profile.get("owner_id"):
                bridge_user_id = primary_config.get("user_id")
                bridge_folder_id = primary_config.get("folder_id")
                if org_id:
                    request.state.profile["owner_id"] = org_id
                    if bridge_folder_id and bridge_user_id:
                        request.state.profile["owner_id"] = f"{org_id}_{bridge_folder_id}_{bridge_user_id}"

            body_wrapper_id = body.get("wrapper_id")
            incoming_response_format = body.get("settings", {}).get("response_format")
            body.update(primary_config)
            if body_wrapper_id is not None:
                body["wrapper_id"] = body_wrapper_id
            if incoming_response_format is not None:
                body.setdefault("settings", {})["response_format"] = incoming_response_format

            explicit_stream = body.get("stream")

            if explicit_stream is not None:
                body.get("configuration", {})["stream"] = explicit_stream

            body["bridge_configurations"] = bridge_configurations
            # `wallet` and `org_billing_plan` already reached the body through
            # body.update(primary_config) above — they are stamped on every cfg.
            body["billing_attribution"] = db_config.get("billing_attribution") or {}
            if credit_hold_token:
                body["credit_hold_token"] = credit_hold_token
            service = body.get("service")
            model = (body.get("configuration") or {}).get("model")
            user = body.get("user")
            images = body.get("images") or [
                url.get("url")
                for url in body.get("user_urls", [])
                if isinstance(url, dict) and url.get("type") == "image" and url.get("url")
            ]
            audios = [
                url.get("url")
                for url in body.get("user_urls", [])
                if isinstance(url, dict) and url.get("type") == "audio" and url.get("url")
            ]
            batch = body.get("batch") or []
            is_rerun = bool(body.get("message_ids")) or bool(body.get("thread_id") and body.get("sub_thread_id"))
            if not is_rerun and user is None and len(images) == 0 and len(audios) == 0 and len(batch) == 0:
                raise HTTPException(status_code=400, detail={"success": False, "error": "User message is compulsory"})
            if not (service in model_config_document and model in model_config_document[service]):
                raise HTTPException(status_code=400, detail={"success": False, "error": "model or service does not exist!"})
            if model_config_document[service][model].get("org_id"):
                if model_config_document[service][model]["org_id"] != org_id:
                    raise HTTPException(
                        status_code=400, detail={"success": False, "error": "model or service does not exist!"}
                    )

            return db_config
        except RequestValidationError:
            # No credit hold yet (validation runs before reserve) — just re-raise for 422 handler.
            raise
        except HTTPException:
            # Validation failures after the reserve (empty bridge_configurations,
            # "User message is compulsory", unknown model, ...) must hand the hold
            # back too — this branch used to skip the release below and leak it.
            if credit_hold_token and org_id:
                await release_credits(org_id, credit_hold_token)
            raise
        except Exception as e:
            if credit_hold_token and org_id:
                await release_credits(org_id, credit_hold_token)
            logger.error(f"Error in get_data: {str(e)}, {traceback.format_exc()}")
            raise HTTPException(
                status_code=400, detail={"success": False, "error": "Error in getting data: " + str(e)}
            ) from e
