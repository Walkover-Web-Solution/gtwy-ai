import asyncio
import json
import traceback
import uuid

from fastapi import FastAPI
from fastapi.responses import JSONResponse, StreamingResponse

from globals import TRANSFER_HISTORY, BadRequestException, logger
from models.mongo_connection import db
from src.configs.constant import alert_types, redis_keys
from src.handler.executionHandler import handle_exceptions
from src.send_alert import send_alert
from src.services.auto_router_service import apply_auto_model_selection
from src.services.cache_service import find_in_cache, store_in_cache
from src.services.todo.planner_service import prepare_planner_request
from src.services.todo.todo_handler import handle_todo_mode
from src.services.utils.common_utils import (
    add_default_template,
    add_files_to_parse_data,
    add_user_in_variables,
    apply_prompt_wrapper,
    build_service_params,
    build_service_params_for_batch,
    configure_custom_settings,
    create_history_params,
    create_latency_object,
    filter_missing_vars,
    handle_agent_transfer,
    handle_fine_tune_model,
    handle_post_tool,
    handle_pre_tools,
    initialize_timer,
    load_model_configuration,
    manage_threads,
    parse_request_body,
    prepare_prompt,
    process_background_tasks,
    process_background_tasks_for_error,
    process_batch_background_tasks,
    process_variable_state,
    render_template_if_applicable,
    restructure_json_schema,
    save_error_history,
    setup_agent_tools,
    sse_stream_and_finalize,
    update_cost_usage_and_apikey_status_in_background,
    update_usage_metrics,
    validate_json_schema_configuration,
)
from src.services.utils.maximum_iterations_utils import (
    build_tool_count_key,
    cleanup_tool_count,
    cleanup_tool_count_after,
    init_tool_count,
)


from ..utils.ai_middleware_format import Response_formatter
from ..utils.helper import Helper
from .baseService.utils import fix_json_string, sendResponse, unknown_error_handler
from .response_caching_service import handle_response_caching
from .reviewer_service import run_review_loop
from src.services.todo.todo_handler import handle_todo_mode
from src.services.todo.planner_service import prepare_planner_request

app = FastAPI()
configurationModel = db["configurations"]


@handle_exceptions
async def chat_multiple_agents(request_body):
    try:
        # Extract bridge configurations from the body
        body = request_body.get("body", {})
        bridge_configurations = body.get("bridge_configurations", {})

        if not bridge_configurations:
            raise ValueError("No bridge configurations found")

        primary_bridge_id = body.get("bridge_id")

        # Check Redis cache for previously used agent with same thread_id and sub_thread_id
        thread_id = body.get("thread_id")
        sub_thread_id = body.get("sub_thread_id") or thread_id

        if thread_id and sub_thread_id:
            redis_key = f"{redis_keys['last_transffered_agent_']}{primary_bridge_id}_{thread_id}_{sub_thread_id}"
            cached_agent_id_raw = await find_in_cache(redis_key)
            # Parse JSON string to remove extra quotes added by store_in_cache
            cached_agent_id = None
            if cached_agent_id_raw:
                try:
                    cached_agent_id = json.loads(cached_agent_id_raw)
                except (json.JSONDecodeError, TypeError):
                    # If parsing fails, use the raw value as fallback
                    cached_agent_id = (
                        cached_agent_id_raw.strip('"') if isinstance(cached_agent_id_raw, str) else cached_agent_id_raw
                    )
            if cached_agent_id and cached_agent_id in bridge_configurations:
                primary_bridge_id = cached_agent_id
                logger.info(f"Using cached agent {cached_agent_id} for thread {thread_id}_{sub_thread_id}")

        if not primary_bridge_id or primary_bridge_id not in bridge_configurations:
            # Use the first agent as primary
            primary_bridge_id = next(iter(bridge_configurations.keys()))

        primary_config = bridge_configurations[primary_bridge_id]

        # Create a new body for the primary agent
        primary_body = body.copy()
        wrapper_id = primary_body.get("wrapper_id")

        # Save request-level values from "configuration" that must survive the
        # bridge-config merge (primary_config["configuration"] is the raw DB config
        # and will fully overwrite primary_body["configuration"] via .update()).
        original_response_format = (body.get("settings") or {}).get("response_format")

        primary_body.update(primary_config)
        primary_body["wrapper_id"] = wrapper_id
        primary_body["bridge_id"] = primary_bridge_id
        # Store the original primary_bridge_id for Redis key consistency
        primary_body["primary_bridge_id"] = primary_bridge_id

        # Restore response_format set at request-level that was clobbered by the bridge-config merge above.
        if original_response_format is not None:
            primary_body.setdefault("settings", {})["response_format"] = original_response_format
            print(f"[chat_multiple_agents] response_format restored: type={original_response_format.get('type')}, channel={original_response_format.get('cred', {}).get('channel')}")

        # Create a complete request_body structure for the primary agent
        primary_request_body = {
            "body": primary_body,
            "state": request_body.get("state", {}).copy(),
            "path_params": request_body.get("path_params", {}),
        }

        # Call the chat function for the primary agent only
        result = await chat(primary_request_body)

        # Return the result directly
        return result

    except Exception as error:
        logger.error(f"Error in chat_multiple_agents: {str(error)}, {traceback.format_exc()}")
        error_object = {
            "success": False,
            "error": f"{str(error)} (Type: {type(error).__name__}). For more support contact us at support@gtwy.ai",
        }
        return JSONResponse(status_code=500, content=error_object)


@handle_exceptions
async def chat(request_body):
    result = {}
    class_obj = {}
    tool_count_key_for_cleanup = None
    tool_count_owner_token = None
    first_execution_error_code = None
    fallback_service = None
    fallback_error_code = None
    completion_success = True
    original_service = None
    try:
        # Store bridge_configurations for potential transfer logic
        bridge_configurations = request_body.get("body", {}).get("bridge_configurations", {})
        # Step 1: Parse and validate request body
        parsed_data = parse_request_body(request_body)

        mcp_cfg = (parsed_data.get("configuration") or {}).get("mcp_config")
        if isinstance(mcp_cfg, dict):
            from src.services.utils.mcp_utils import resolve_mcp_type
            mcp_type = resolve_mcp_type(parsed_data.get("service"), parsed_data.get("model"))
            if mcp_type == "client":
                from src.services.mcp_gateway.prefetch import prefetch_mcp_tools
                await prefetch_mcp_tools(mcp_cfg)

        # Reviewer agent: if this bridge has a configured reviewer, stash its
        # bridge_id on parsed_data. The review loop runs after the main agent
        # fully resolves — both the streaming finalizer and the non-streaming
        # path invoke run_review_loop just before history publish. Streaming is
        # preserved when requested; the reviewer's tokens flow onto the same
        # SSE connection so the user sees the verdict in real time.
        reviewer_bridge_id = (
            bridge_configurations.get(parsed_data["bridge_id"], {}).get("reviewer_agent") or ""
        )
        if reviewer_bridge_id and reviewer_bridge_id in bridge_configurations:
            parsed_data["_reviewer_bridge_id"] = reviewer_bridge_id

        tool_count_key_for_cleanup = build_tool_count_key(
            parsed_data.get("bridge_id"),
            parsed_data.get("message_id"),
        )
        # Claim ownership of the tool-call counter for this agent invocation.
        # If a child instance (agent-as-tool recursion) re-enters with the same
        # bridge_id+message_id, it gets token=None and therefore cannot wipe
        # the parent's counter in its own finally block.
        tool_count_owner_token = init_tool_count(
            tool_count_key_for_cleanup,
            parsed_data.get("maximum_iterations") or 3,
        )

        # To maintain the API Key status for the original service, because it gets overrited when Fallback is used
        original_service = parsed_data["service"]

        process_variable_state(parsed_data)

        # Setup pre_tools and post_tool for the current agent with its own variables
        current_agent_id = parsed_data.get("bridge_id")
        pre_tools = bridge_configurations.get(current_agent_id, {}).get("pre_tools_data", [])
        post_tool = bridge_configurations.get(current_agent_id, {}).get("post_tool_data", {})
        parsed_data["pre_tools"] = setup_agent_tools(parsed_data, bridge_configurations, pre_tools)
        parsed_data["post_tool_data"] = setup_agent_tools(parsed_data, bridge_configurations, post_tool)
        await apply_prompt_wrapper(parsed_data)

        # Initialize or retrieve transfer_request_id for tracking transfers
        transfer_request_id = parsed_data.get("transfer_request_id") or str(uuid.uuid1())
        parsed_data["transfer_request_id"] = transfer_request_id

        # Initialize transfer history for this request if not exists
        if transfer_request_id not in TRANSFER_HISTORY:
            TRANSFER_HISTORY[transfer_request_id] = []

        parsed_data["configuration"]["prompt"] = add_default_template(
            parsed_data.get("configuration", {}).get("prompt", "")
        )
        parsed_data["variables"] = add_user_in_variables(parsed_data["variables"], parsed_data["user"])
        # Step 2: Initialize Timer
        timer = initialize_timer(parsed_data["state"])

        # Check Auto Model Selection
        if parsed_data.get("auto_model_select", False):
            await apply_auto_model_selection(parsed_data, timer)

        # Step 3: Load Model Configuration
        model_config, custom_config, model_output_config = await load_model_configuration(
            parsed_data["model"],
            parsed_data["configuration"],
            parsed_data["service"],
        )
        # Step 3: Load Model Configuration
        await handle_fine_tune_model(parsed_data, custom_config)

        # Step 4: Handle Pre-Tools Execution
        await handle_pre_tools(parsed_data,custom_config)

        # Step 5: Manage Threads
        thread_info = await manage_threads(parsed_data)
        # add Files from cache is Present
        if len(parsed_data["files"]) == 0:
            parsed_data["files"] = await add_files_to_parse_data(
                parsed_data["thread_id"], parsed_data["sub_thread_id"], parsed_data["bridge_id"]
            )


        # Step 7: Prepare Prompt, Variables and Memory
        memory, missing_vars = await prepare_prompt(parsed_data, thread_info, model_config, custom_config)

        missing_vars = filter_missing_vars(missing_vars, parsed_data["variables_state"])

        # Handle missing variables
        if missing_vars and not parsed_data.get("is_playground"):
            asyncio.create_task(send_alert(
                bridge_id=parsed_data["bridge_id"],
                org_id=parsed_data["org_id"],
                error_log=missing_vars,
                error_type=alert_types["variable"],
                bridge_name=parsed_data.get("name"),
                org_name=parsed_data.get("org_name"),
                is_embed=parsed_data.get("is_embed"),
                user_id=parsed_data.get("user_id"),
                thread_id=parsed_data.get("thread_id"),
                service=parsed_data.get("service"),
                api_collection=parsed_data.get("api_collection"),
                is_external_error=False,
            ))

        # Step 8: Configure Custom Settings
        custom_config = await configure_custom_settings(
            model_config["configuration"], custom_config, parsed_data["service"]
        )
        response_type = parsed_data.get("response_type") or {}
        template_requested = isinstance(response_type, dict) and response_type.get("is_template", False)
        if template_requested:
            parsed_data.setdefault("flags", {})["template_requested"] = True
            # When templates are requested we still allow streaming, but emit a dedicated
            # template_response event (handled inside sse_stream_and_finalize).

        # Step 9: Execute Service Handler
        params = build_service_params(
            parsed_data,
            custom_config,
            model_output_config,
            thread_info,
            timer,
            memory,
            bridge_configurations,
        )
        # Step 10: json_schema service conversion
        is_valid_schema, schema_error = validate_json_schema_configuration(custom_config)
        if not is_valid_schema:
            raise ValueError(schema_error)

        if "response_type" in custom_config and isinstance(custom_config["response_type"], dict) and custom_config["response_type"].get("type") == "json_schema":
            custom_config["response_type"] = restructure_json_schema(
                custom_config["response_type"], parsed_data["service"]
            )
        if parsed_data.get("mode") == "plan":
            # Executor orchestration actions keep the existing pipeline.
            # The planner LLM call (no action) falls through to chat() with
            # custom_config mutated to inject the planner prompt + json output.
            if parsed_data.get("action"):
                return await handle_todo_mode(
                    parsed_data=parsed_data,
                    bridge_configurations=bridge_configurations,
                )
            await prepare_planner_request(parsed_data, bridge_configurations, custom_config)

        # Execute with retry mechanism
        class_obj = await Helper.create_service_handler(params, parsed_data["service"])

        # If this request was a streamed agent transfer, reuse the existing SSE connection
        injected_streamer = (request_body.get("body", {}) if isinstance(request_body, dict) else {}).get("_injected_streamer")
        if injected_streamer and getattr(class_obj, "stream_mode", False):
            class_obj.streamer = injected_streamer

        # Streaming is SSE-only: if stream=true return StreamingResponse.
        if getattr(class_obj, "stream_mode", False) and getattr(class_obj, "streamer", None):
            # Plan mode (planner call): frontend expects `start` -> `planning`
            # -> deltas. Emit both up-front so ordering matches the legacy
            # planner; baseService.stream's own emit_start is idempotent.
            if parsed_data.get("mode") == "plan" and not parsed_data.get("action"):
                await class_obj.streamer.emit_start(
                    model=parsed_data.get("model") or "",
                    service=parsed_data.get("service") or "",
                    bridge_id=str(parsed_data.get("bridge_id") or ""),
                    message_id=str(parsed_data.get("message_id") or ""),
                )
                await class_obj.streamer.emit_planning()
            sync_injected_stream_call = bool(
                (request_body.get("body", {}) if isinstance(request_body, dict) else {}).get("_sync_injected_stream_call")
            )
            if injected_streamer:
                if sync_injected_stream_call:
                    return await sse_stream_and_finalize(
                        class_obj, parsed_data, params, timer, thread_info, transfer_request_id, bridge_configurations,
                        request_body=request_body, chat_function=chat,
                    )
                # Agent-transfer: existing SSE connection owned by the caller — background task, no new StreamingResponse
                _transfer_task = asyncio.create_task(sse_stream_and_finalize(
                    class_obj, parsed_data, params, timer, thread_info, transfer_request_id, bridge_configurations,
                    request_body=request_body, chat_function=chat,
                ))
                # Defer tool-count cleanup until the background task finishes so the
                # iteration limit is enforced for the streamed run instead of being
                # wiped by chat()'s own finally before the loop executes.
                asyncio.create_task(cleanup_tool_count_after(
                    _transfer_task, tool_count_key_for_cleanup, tool_count_owner_token,
                ))
                tool_count_owner_token = None  # ownership transferred to the deferred cleanup
                return JSONResponse(status_code=200, content={"success": True})

            _stream_task = asyncio.create_task(sse_stream_and_finalize(
                class_obj, parsed_data, params, timer, thread_info, transfer_request_id, bridge_configurations,
                request_body=request_body, chat_function=chat,
            ))
            # See note above: defer cleanup so streaming honors maximum_iterations.
            asyncio.create_task(cleanup_tool_count_after(
                _stream_task, tool_count_key_for_cleanup, tool_count_owner_token,
            ))
            tool_count_owner_token = None  # ownership transferred to the deferred cleanup
            return StreamingResponse(class_obj.streamer.generator(), media_type="text/event-stream")

        original_exception = None
        try:
            result = await handle_response_caching(parsed_data=parsed_data,class_obj=class_obj)

            # If the model was configured to return JSON, validate and repair the content.
            # Types "json_object" and "json_schema" imply the content must be parsable JSON.
            _rt = custom_config.get("response_type") or {}
            if isinstance(_rt, dict) and _rt.get("type") in ("json_object", "json_schema"):
                _content = (result.get("response") or {}).get("data", {}).get("content")
                if isinstance(_content, str):
                    try:
                        json.loads(_content)
                    except (json.JSONDecodeError, ValueError):
                        try:
                            _repaired = fix_json_string(_content)
                            result["response"]["data"]["content"] = _repaired
                        except Exception as _json_err:
                            asyncio.create_task(unknown_error_handler({
                                "error": f"Model returned invalid JSON: {str(_json_err)}",
                                "raw_content": _content[:500],
                                "model": parsed_data.get("model"),
                                "service": parsed_data.get("service"),
                                "bridge_id": parsed_data.get("bridge_id"),
                                "org_id": parsed_data.get("org_id"),
                                "message_id": parsed_data.get("message_id"),
                                "response_type": _rt.get("type"),
                            }))
                            result["success"] = False
                            original_error = f"Model returned invalid JSON: {_content[:200]}"

            # Check if agent transfer is needed
            transfer_agent_config = result.get("transfer_agent_config")
            if transfer_agent_config and transfer_agent_config.get("action_type") == "transfer":
                # Get the correct version_id from bridge_configurations for this agent
                current_version_id = bridge_configurations.get(parsed_data["bridge_id"], {}).get(
                    "version_id", parsed_data["version_id"]
                )

                # Calculate tokens and create latency BEFORE storing history for transfer
                if parsed_data.get("type") != "image":
                    parsed_data["tokens"] = params["token_calculator"].calculate_total_cost(
                        parsed_data["model"], parsed_data["service"]
                    )
                    result["response"]["usage"]["cost"] = parsed_data["tokens"].get("total_cost") or 0

                # Create latency and update usage metrics BEFORE storing history for transfer
                latency = create_latency_object(timer, params)
                update_usage_metrics(parsed_data, params, latency, result=result, success=True)

                # Store current agent's history data before transferring
                current_history_data = {
                    "bridge_id": parsed_data["bridge_id"],
                    "history_params": result.get("historyParams"),
                    "dataset": [parsed_data["usage"]],
                    "version_id": current_version_id,
                    "thread_info": thread_info,
                    "parent_id": parsed_data.get("parent_bridge_id", ""),
                }
                TRANSFER_HISTORY[transfer_request_id].append(current_history_data)

                # Handle agent transfer
                transfer_result = await handle_agent_transfer(
                    result,
                    request_body,
                    bridge_configurations,
                    chat,
                    current_bridge_id=parsed_data["bridge_id"],
                    transfer_request_id=transfer_request_id,
                )
                if transfer_result is not None:
                    return transfer_result

            result["response"]["usage"] = params["token_calculator"].get_total_usage()
            execution_failed = not result["success"]
            _raw_error = result.get("error", "Unknown error") if execution_failed else None
            original_error = str(_raw_error) if _raw_error is not None and not isinstance(_raw_error, str) else _raw_error
        except Exception as execution_exception:
            # Handle exceptions during execution
            execution_failed = True
            original_error = str(execution_exception)
            first_execution_error_code = getattr(execution_exception, "status_code", None)
            original_exception = execution_exception
            logger.error(
                f"Initial execution failed with {parsed_data['service']}/{parsed_data['model']}: {original_error}"
            )
            result = {"success": False, "error": original_error, "response": {"usage": {}}, "modelResponse": {}}

        # Retry mechanism with fallback configuration
        if execution_failed and parsed_data.get("settings", {}).get("fall_back") and parsed_data["settings"]["fall_back"].get("is_enable", False):
            try:
                # Store original configuration
                fallback_config = parsed_data["settings"]["fall_back"]
                original_model = parsed_data["model"]

                # Update parsed_data with fallback configuration
                fallback_model = fallback_config.get("model", parsed_data["model"])
                fallback_service = fallback_config.get("service", parsed_data["service"])
                parsed_data["model"] = fallback_model
                parsed_data["service"] = fallback_service
                parsed_data["configuration"]["model"] = fallback_model
                if fallback_config.get("apikey"):
                    parsed_data["apikey"] = fallback_config["apikey"]

                # Always rebuild fallback handler/config to avoid stale customConfig/model reuse
                (
                    fallback_model_config,
                    fallback_custom_config,
                    fallback_model_output_config,
                ) = await load_model_configuration(
                    parsed_data["model"], parsed_data["configuration"], parsed_data["service"]
                )

                fallback_custom_config = await configure_custom_settings(
                    fallback_model_config["configuration"], fallback_custom_config, parsed_data["service"]
                )
                params = build_service_params(
                    parsed_data,
                    fallback_custom_config,
                    fallback_model_output_config,
                    thread_info,
                    timer,
                    memory,
                    bridge_configurations,
                )

                if (
                    "response_type" in fallback_custom_config
                    and fallback_custom_config["response_type"].get("type") == "json_schema"
                ):
                    fallback_custom_config["response_type"] = restructure_json_schema(
                        fallback_custom_config["response_type"], parsed_data["service"]
                    )

                class_obj = await Helper.create_service_handler(params, parsed_data["service"])

                # Execute with updated configuration
                result = await class_obj.execute()
                result["response"]["usage"] = params["token_calculator"].get_total_usage()

                # Mark that this was a retry attempt and store original error
                if result["success"]:
                    firstAttemptError = f"Original attempt failed with {original_service}/{original_model}: {original_error}. Retried with {parsed_data['service']}/{parsed_data['model']}"
                    result["response"]["data"]["firstAttemptError"] = firstAttemptError
                    result["historyParams"]["firstAttemptError"] = firstAttemptError
                    result["response"]["data"]["fallback"] = True

            except Exception as retry_error:
                # If retry also fails, chain the new exception to the original one
                logger.error(
                    f"Fallback attempt failed with {parsed_data['service']}/{parsed_data['model']}: {retry_error}"
                )
                fallback_service = parsed_data["service"]
                fallback_error_code = getattr(retry_error, "status_code", None)
                # Restore original configuration before raising
                parsed_data["model"] = original_model
                parsed_data["service"] = original_service
                parsed_data["firstAttemptError"] = (
                        f"Original attempt failed with {original_service}/{original_model}: {original_error}. Retried with {fallback_config['service']}/{fallback_config['model']}"
                    )
                raise retry_error from original_exception

        if not result["success"]:
            raise ValueError(result)
        # Add message_id to response
        result["response"]["data"]["message_id"] = parsed_data["message_id"]
        if getattr(class_obj, 'maximum_iteration_limit_reached', False):
            result["response"]["data"]["finish_reason"] = "maximum_iteration_limit_reached"
        if result.get("error"):
            result["response"]["error"] = result["error"]

        if original_error and not parsed_data.get("is_playground"):
            asyncio.create_task(send_alert(
                bridge_id=parsed_data["bridge_id"],
                org_id=parsed_data["org_id"],
                error_log=original_error,
                error_type=alert_types["retry_mechanism"],
                bridge_name=parsed_data.get("name"),
                org_name=parsed_data.get("org_name"),
                is_embed=parsed_data.get("is_embed"),
                user_id=parsed_data.get("user_id"),
                thread_id=parsed_data.get("thread_id"),
                service=parsed_data.get("service"),
                api_collection=parsed_data.get("api_collection"),
                is_external_error=False,
            ))

        parsed_data["alert_flag"] = result["modelResponse"].get("alert_flag", False)
        if parsed_data.get("type") != "image":
            parsed_data["tokens"] = params["token_calculator"].calculate_total_cost(
                parsed_data["model"], parsed_data["service"]
            )
            result["response"]["usage"]["cost"] = parsed_data["tokens"].get("total_cost") or 0

        # Template HTML Generation
        template_data = render_template_if_applicable(parsed_data, result)
        # Add template data to historyParams chatbot_message
        if template_data and result.get('historyParams'):
            result['historyParams']['chatbot_message'] = json.dumps(result['response']['data']['content'])

        # Create latency object using utility function
        latency = create_latency_object(timer, params)
        # Update usage metrics for round 1 (and run the reviewer loop) BEFORE the
        # playground/non-playground split so both paths see the final, reviewed
        # response with summed tokens. Webhook / RTLayer / playground sendResponse
        # destinations downstream get the *final* response, not the round-1 draft.
        update_usage_metrics(parsed_data, params, latency, result=result, success=True)
        result.setdefault("response", {}).setdefault("usage", {})
        result["response"]["usage"]["cost"] = parsed_data["usage"].get("expectedCost", 0)

        # Reviewer agent loop: if a reviewer is configured for this bridge,
        # iterate up to MAX_REVIEW_ROUNDS (review → optional re-run → review).
        # Tokens from all main-agent attempts are summed into parsed_data["usage"]
        # so the main agent's conversation_log row reflects cumulative cost.
        # The reviewer publishes its own conversation_log row from inside
        # run_review_loop — independent of process_background_tasks below.
        if parsed_data.get("_reviewer_bridge_id"):
            result, _reviewer_summary = await run_review_loop(
                parsed_data=parsed_data,
                params=params,
                timer=timer,
                thread_info=thread_info,
                bridge_configurations=bridge_configurations,
                main_result=result,
                memory=memory,
            )
            # Refresh client-visible cost field after summing rounds.
            result.setdefault("response", {}).setdefault("usage", {})
            result["response"]["usage"]["cost"] = parsed_data["usage"].get("expectedCost", 0)

        # Run post_tool for both playground and non-playground
        post_tool_response = await handle_post_tool(parsed_data, result)
        if post_tool_response and post_tool_response.get("status") == 1 and post_tool_response.get("response") is not None:
            result["response"]["data"]["content"] = post_tool_response.get("response")

        # Reuse the latency captured before the review loop — the main timer was
        # already consumed there, so re-calling create_latency_object would yield
        # over_all_time=0 and overwrite the correct value. Update execution_time_logs
        # in-place so any re-run entries accumulated during the review loop are included.
        latency["execution_time_logs"] = params.get("execution_time_logs") or {}
        latency["model_execution_time"] = (
            sum(log.get("time_taken", 0) for log in (params.get("execution_time_logs") or [])) or ""
        )
        if result.get("response") and result["response"].get("data"):
            result["response"]["data"]["message_id"] = parsed_data["message_id"]
        await sendResponse(
            parsed_data["response_format"],
            result["response"],
            success=True,
            variables=parsed_data.get("variables", {}),
        )
        # Update usage metrics for successful API calls
        update_usage_metrics(parsed_data, params, latency, result=result, success=True)
        result["response"]["usage"]["cost"] = parsed_data["usage"].get("expectedCost", 0)

        # If testcase_data is present, score the result and optionally push to RTLayer
        if parsed_data.get("testcase_data", {}).get("run_testcase", False):
            from src.services.commonServices.testcases import process_single_testcase_result
            testcase_result = await process_single_testcase_result(
                parsed_data.get("testcase_data", {}), result, parsed_data
            )
            result["response"]["testcase_result"] = testcase_result

            # Stamp the testcase fields onto historyParams so the log queue
            # persists them onto the conversation_logs row in Postgres.
            if result.get("historyParams") is not None:
                result["historyParams"]["testcase_id"] = testcase_result.get("testcase_id")
                result["historyParams"]["testcase_data"] = {
                    "expected": testcase_result.get("expected"),
                    "actual": testcase_result.get("actual"),
                    "score": testcase_result.get("score"),
                    "matching_type": testcase_result.get("matching_type"),
                    "type": testcase_result.get("type"),
                    "success": testcase_result.get("success"),
                    "error": testcase_result.get("error"),
                    "system_prompt": parsed_data.get("configuration", {}).get("prompt", ""),
                    "model": parsed_data.get("configuration", {}).get("model", ""),
                }

            if parsed_data.get("body", {}).get("bridge_configurations", {}).get("playground_response_format"):
                await sendResponse(
                    parsed_data["body"]["bridge_configurations"]["playground_response_format"],
                    parsed_data["testcase_data"],
                    success=True,
                    variables=parsed_data.get("variables", {}),
                    meta=parsed_data.get("meta"),
            )

        if parsed_data.get('pre_tool_response_to_save') and result['historyParams'] is not None:
            result['historyParams']['tools_call_data'].append(parsed_data['pre_tool_response_to_save'])

        # Process background tasks (handles both transfer and non-transfer cases)
        await process_background_tasks(
            parsed_data, result, params, thread_info, transfer_request_id, bridge_configurations
        )

        # Save agent bridge_id to Redis for 3 days (259200 seconds)
        thread_id = parsed_data.get("thread_id")
        sub_thread_id = parsed_data.get("sub_thread_id")
        bridge_id = parsed_data.get("bridge_id")
        original_primary_bridge_id = request_body.get("body", {}).get("primary_bridge_id")

        if thread_id and sub_thread_id and bridge_id:
            # Use original primary bridge_id in key for consistency, but save current bridge_id as value
            redis_key = (
                f"{redis_keys['last_transffered_agent_']}{original_primary_bridge_id}_{thread_id}_{sub_thread_id}"
            )
            # Ensure bridge_id is a clean string without extra quotes
            bridge_id_to_save = str(bridge_id).strip("\"'") if bridge_id else None
            if bridge_id_to_save:
                await store_in_cache(redis_key, bridge_id_to_save, ttl=259200)  # 3 days
            logger.info(
                f"Cached agent {bridge_id} for thread {thread_id}_{sub_thread_id} with key based on original primary {original_primary_bridge_id}"
            )

        return JSONResponse(status_code=200, content={"success": True, "response": result["response"]})

    except (Exception, ValueError, BadRequestException) as error:
        if not isinstance(error, BadRequestException):
            logger.error(f"Error in chat service: %s, {str(error)}, {traceback.format_exc()}")
        await sendResponse(
            parsed_data["response_format"], result.get("error", str(error)), variables=parsed_data["variables"], meta=parsed_data.get("meta")
        ) if parsed_data["response_format"]["type"] != "default" else None
        # save_error_history builds latency + usage metrics + historyParams and
        # publishes a single error history row. parsed_data["firstAttemptError"]
        # set during fallback retry (above) flows into the row via
        # create_history_params.
        await save_error_history(parsed_data, error, params, timer, class_obj)
        # Check for a chained exception and create a structured error object
        if error.__cause__:
            # Combine both initial and fallback errors into a single string
            combined_error_string = (
                f"Initial Error: {str(error.__cause__)} (Type: {type(error.__cause__).__name__}). "
                f"Fallback Error: {str(error)} (Type: {type(error).__name__}). "
                f"For more support contact us at support@gtwy.ai"
            )
            error_object = {
                "success": False,
                "error": combined_error_string,
                "message_id": parsed_data.get("message_id"),
            }
        else:
            # Single error case
            error_string = (
                f"{str(error)} (Type: {type(error).__name__}). For more support contact us at support@gtwy.ai"
            )
            error_object = {"success": False, "error": error_string, "message_id": parsed_data.get("message_id")}
        completion_success = False
        raise ValueError(error_object) from None
    finally:
        cleanup_tool_count(tool_count_key_for_cleanup, tool_count_owner_token)
        await update_cost_usage_and_apikey_status_in_background(
            original_service,
            parsed_data,
            first_execution_error_code,
            completion_success,
            fallback_service,
            fallback_error_code,
        )


@handle_exceptions
async def embedding(request_body):
    result = {}
    try:
        body = request_body.get("body")
        configuration = body.get("configuration")
        text = body.get("text")
        model = configuration.get("model")
        service = body.get("service")
        model_config, custom_config, model_output_config = await load_model_configuration(model, configuration, service)
        chatbot = body.get("chatbot")
        if chatbot:
            raise ValueError("Error: Embedding not supported for chatbot")
        params = {
            "model": model,
            "configuration": configuration,
            "model_config": model_config,
            "customConfig": custom_config,
            "model_output_config": model_output_config,
            "text": text,
            "response_format": body.get("settings", {}).get("response_format") or {},
            "service": service,
            "version_id": body.get("version_id"),
            "bridge_id": body.get("bridge_id"),
            "org_id": body.get("org_id"),
            "apikey": body.get("apikey"),
        }

        class_obj = await Helper.embedding_service_handler(params, service)
        result = await class_obj.execute_embedding()

        if not result["success"]:
            raise ValueError(result)

        result["modelResponse"] = await Response_formatter(
            response=result["response"], service=service, type=configuration.get("type")
        )

        return JSONResponse(status_code=200, content={"success": True, "response": result["modelResponse"]})
    except Exception as error:
        raise ValueError(error) from error


@handle_exceptions
async def batch(request_body):
    result = {}
    class_obj = {}
    try:
        # Step 1: Parse and validate request body
        parsed_data = parse_request_body(request_body)
        if parsed_data["batch_webhook"] is None:
            raise ValueError("webhook is required")

        # Manage threads (set thread_id / sub_thread_id when not in body)
        await manage_threads(parsed_data)

        # Validate batch_variables if provided
        batch_variables = parsed_data.get("batch_variables")
        if batch_variables is not None:
            if not isinstance(batch_variables, list):
                raise ValueError("batch_variables must be an array")
            if len(batch_variables) != len(parsed_data["batch"]):
                raise ValueError(
                    f"batch_variables array length ({len(batch_variables)}) must match batch array length ({len(parsed_data['batch'])})"
                )

        # Step 2: Process prompts with variable replacement for each batch message
        original_prompt = parsed_data["configuration"].get("prompt", "")
        processed_prompts = []
        all_missing_vars = {}

        if batch_variables is not None:
            for _idx, variables in enumerate(batch_variables):
                # Replace variables in prompt for each message
                # If a variable is not provided, the placeholder remains in the prompt
                processed_prompt, missing_vars = Helper.replace_variables_in_prompt(original_prompt, variables)
                processed_prompts.append(processed_prompt)

                # Collect missing variables from all batch items
                if missing_vars:
                    for key, value in missing_vars.items():
                        if key not in all_missing_vars:
                            all_missing_vars[key] = value
        else:
            # No batch_variables provided, use original prompt for all messages
            for _ in parsed_data["batch"]:
                processed_prompts.append(original_prompt)

        # Send alert if there are any missing variables across all batch items
        if all_missing_vars and not parsed_data.get("is_playground"):
            asyncio.create_task(send_alert(
                bridge_id=parsed_data["bridge_id"],
                org_id=parsed_data["org_id"],
                error_log=all_missing_vars,
                error_type=alert_types["variable"],
                bridge_name=parsed_data.get("name"),
                org_name=parsed_data.get("org_name"),
                is_embed=parsed_data.get("is_embed"),
                user_id=parsed_data.get("user_id"),
                thread_id=parsed_data.get("thread_id"),
                service=parsed_data.get("service"),
                api_collection=parsed_data.get("api_collection"),
                is_external_error=False,
            ))

        # Store processed prompts in parsed_data
        parsed_data["processed_prompts"] = processed_prompts

        # Step 3: Load Model Configuration
        model_config, custom_config, model_output_config = await load_model_configuration(
            parsed_data["model"],
            parsed_data["configuration"],
            parsed_data["service"],
        )

        # Step 4: Handle Pre-Tools Execution
        await handle_pre_tools(parsed_data, custom_config)

        # Step 7: Configure Custom Settings
        custom_config = await configure_custom_settings(
            model_config["configuration"], custom_config, parsed_data["service"]
        )
        if "tools" in custom_config:
            del custom_config["tools"]

        # json_schema service conversion (mirrors chat function Step 10)
        is_valid_schema, schema_error = validate_json_schema_configuration(custom_config)
        if not is_valid_schema:
            raise ValueError(schema_error)

        if "response_type" in custom_config and isinstance(custom_config["response_type"], dict) and custom_config["response_type"].get("type") == "json_schema":
            custom_config["response_type"] = restructure_json_schema(
                custom_config["response_type"], parsed_data["service"]
            )

        # Step 8: Execute Service Handler
        params = build_service_params_for_batch(parsed_data, custom_config, model_output_config)
        class_obj = await Helper.create_service_handler_for_batch(params, parsed_data["service"])
        result = await class_obj.batch_execute()

        if not result["success"]:
            raise ValueError(result)

        # Store custom_config as AiConfig for batch conversation logs
        parsed_data["ai_config_mapping"] = result["ai_config_mapping"]

        if parsed_data.get('pre_tool_response_to_save') and result['historyParams'] is not None:
                result['historyParams']['tools_call_data'].append(parsed_data['pre_tool_response_to_save'])


        # Step 9: Process batch conversation logs in background
        await process_batch_background_tasks(
            parsed_data=parsed_data,
            result=result,
            processed_prompts=processed_prompts,
            batch_variables=batch_variables
        )

        response_content = {
            "success": True,
            "response": result["message"]
        }

        # Include batch_id and messages if available
        if "batch_id" in result:
            response_content["batch_id"] = result["batch_id"]
        if "messages" in result:
            response_content["messages"] = result["messages"]

        return JSONResponse(status_code=200, content=response_content)
    except Exception as error:
        traceback.print_exc()
        raise ValueError(error) from error


@handle_exceptions
async def image(request_body):
    result = {}
    class_obj = {}
    try:
        # Store bridge_configurations for potential transfer logic
        bridge_configurations = request_body.get("body", {}).get("bridge_configurations", {})

        # Step 1: Parse and validate request body
        parsed_data = parse_request_body(request_body)

        # Initialize or retrieve transfer_request_id for tracking transfers
        transfer_request_id = parsed_data.get("transfer_request_id") or str(uuid.uuid1())
        parsed_data["transfer_request_id"] = transfer_request_id

        # Initialize transfer history for this request if not exists
        if transfer_request_id not in TRANSFER_HISTORY:
            TRANSFER_HISTORY[transfer_request_id] = []

        # Step 2: Initialize Timer
        timer = initialize_timer(parsed_data["state"])

        # Step 3: Load Model Configuration
        model_config, custom_config, model_output_config = await load_model_configuration(
            parsed_data["model"],
            parsed_data["configuration"],
            parsed_data["service"],
        )

        # Step 4: Configure Custom Settings
        custom_config = await configure_custom_settings(
            model_config["configuration"], custom_config, parsed_data["service"]
        )
        # Step 5: Manage Threads
        thread_info = await manage_threads(parsed_data)

        # Step 5: Execute Service Handler
        params = build_service_params(
            parsed_data,
            custom_config,
            model_output_config,
            thread_info,
            timer,
            None,
            bridge_configurations,
        )

        class_obj = await Helper.create_service_handler(params, parsed_data["service"])
        result = await class_obj.execute()

        if not result["success"]:
            raise ValueError(result)

        # Calculate image tokens and costs
        params['token_calculator'].calculate_image_usage(result['response'])

        parsed_data['tokens'] = params['token_calculator'].calculate_image_cost(parsed_data['model'])

        # Add usage data to response
        result['response']['usage'] = params['token_calculator'].get_image_usage()
        result['response']['usage']['cost'] = parsed_data['tokens'].get('total_cost', 0)

        # Create latency object using utility function
        if result.get("response") and result["response"].get("data"):
            result["response"]["data"]["id"] = parsed_data["message_id"]
        await sendResponse(
            parsed_data["response_format"], result["response"], success=True, variables=parsed_data.get("variables", {}), meta=parsed_data.get("meta")
        )
        latency = create_latency_object(timer, params)
        # Update usage metrics for successful API calls
        update_usage_metrics(parsed_data, params, latency, result=result, success=True)
        # Process background tasks (handles both transfer and non-transfer cases)
        await process_background_tasks(
            parsed_data, result, params, thread_info, transfer_request_id, bridge_configurations
        )
        return JSONResponse(status_code=200, content={"success": True, "response": result["response"]})

    except (Exception, ValueError, BadRequestException) as error:
        if not isinstance(error, BadRequestException):
            logger.error(f"Error in image service: {str(error)}, {traceback.format_exc()}")
        # Update parsed_data with thread_info if available and thread_id/sub_thread_id are None
        if "thread_info" in locals() and thread_info:
            if not parsed_data.get("thread_id") and thread_info.get("thread_id"):
                parsed_data["thread_id"] = thread_info["thread_id"]
            if not parsed_data.get("sub_thread_id") and thread_info.get("sub_thread_id"):
                parsed_data["sub_thread_id"] = thread_info["sub_thread_id"]

        await sendResponse(
            parsed_data["response_format"], result.get("error", str(error)), variables=parsed_data["variables"], meta=parsed_data.get("meta")
        ) if parsed_data["response_format"]["type"] != "default" else None
        # save_error_history builds latency + usage metrics + historyParams and
        # publishes a single error history row (with firstAttemptError populated
        # from parsed_data when fallback was attempted).
        await save_error_history(
            parsed_data, error, params, timer, class_obj, thread_info if "thread_info" in locals() else None
        )
        # Check for a chained exception and create a structured error object
        if error.__cause__:
            # Combine both initial and fallback errors into a single string
            combined_error_string = (
                f"Initial Error: {str(error.__cause__)} (Type: {type(error.__cause__).__name__}). "
                f"Fallback Error: {str(error)} (Type: {type(error).__name__}). "
                f"For more support contact us at support@gtwy.ai"
            )
            error_object = {
                "success": False,
                "error": combined_error_string,
                "message_id": parsed_data.get("message_id"),
            }
        else:
            # Single error case
            error_string = (
                f"{str(error)} (Type: {type(error).__name__}). For more support contact us at support@gtwy.ai"
            )
            error_object = {"success": False, "error": error_string, "message_id": parsed_data.get("message_id")}
        raise ValueError(error_object) from None
