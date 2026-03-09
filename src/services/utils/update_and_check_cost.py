import asyncio
import json
import logging
from datetime import datetime

from src.configs.constant import limit_types, redis_keys

from ..cache_service import delete_in_cache, find_in_cache, store_in_cache, verify_ttl
from .apiservice import fetch
from .limit_ttl_utils import calculate_limit_ttl

_THRESHOLD_WEBHOOK_URL = "https://flow.sokt.io/func/scri87toel4G"

logger = logging.getLogger(__name__)


def _build_limit_error(limit_type, current_usage, limit_value):
    """Helper to build a standard limit exceeded payload."""
    return {
        "success": False,
        "error": f"{limit_type.capitalize()} limit exceeded. Used: {current_usage}/{limit_value}",
        "error_code": f"{limit_type.upper()}_LIMIT_EXCEEDED",
        "limit_type": limit_type,
        "current_usage": current_usage,
        "limit_value": limit_value,
    }


async def _check_limit(limit_type, bridges, version_id):
    """Check a specific limit type against the provided bridges dict with Redis cache first.
    
    Args:
        bridges: Flat bridges dict from agent_data['bridges']
        version_id: Current version id
    """
    limit_field = f"{limit_type}_limit"
    usage_field = f"{limit_type}_usage"
    # For version documents, _id is the version id; parent_id holds the actual bridge id.
    # Bridge limit tracking must use the parent bridge id so all versions share one counter.
    version_doc_id = bridges.get("_id")
    parent_bridge_id = bridges.get("parent_id")
    bridge_id = parent_bridge_id or version_doc_id
    service = bridges.get("service")
    apikey_src = bridges.get("apikeys", {}).get(service) or bridges.get("folder_apikeys", {}).get(service) or {}

    # Get limit value
    try:
        if limit_type == "apikey":
            limit_value = float(apikey_src.get(limit_field, 0) or 0)
        else:
            limit_value = float(bridges.get(limit_field, 0) or 0)
    except (ValueError, TypeError):
        limit_value = 0.0

    # Skip if no limit is set
    if limit_value <= 0:
        return None

    # Determine the identifier for Redis key
    if limit_type == "bridge":
        identifier = bridge_id
    elif limit_type == "folder":
        identifier = bridges.get("folder_id")
    elif limit_type == "apikey":
        identifier = (bridges.get("apikey_object_id") or {}).get(service)
    else:
        identifier = None

    usage_value = 0.0

    if identifier:
        cache_key = f"{redis_keys[f'{limit_type}usedcost_']}{identifier}"
        try:
            cached_data = await find_in_cache(cache_key)
            if cached_data:
                currentusagedata = json.loads(cached_data)
                usage_value = float(currentusagedata.get("usage_value", 0))

                versions = currentusagedata.get("versions", [])
                bridge_list = currentusagedata.get("bridges", [])

                if version_id and version_id not in versions:
                    versions.append(version_id)

                if bridge_id and bridge_id not in bridge_list:
                    bridge_list.append(bridge_id)

                reset_period = currentusagedata.get("reset_period") or "monthly"
                ttl = calculate_limit_ttl(reset_period)

                if limit_type == "bridge":
                    updated_data = {"usage_value": usage_value, "versions": versions, "reset_period": reset_period}
                else:
                    updated_data = {"usage_value": usage_value, "versions": versions, "bridges": bridge_list, "reset_period": reset_period}

                await store_in_cache(cache_key, updated_data, ttl=ttl)
            else:
                # No Redis cache — seed from bridge data
                try:
                    if limit_type == "apikey":
                        usage_value = float(apikey_src.get(usage_field, 0) or 0)
                    else:
                        usage_value = float(bridges.get(usage_field, 0) or 0)
                except (ValueError, TypeError):
                    usage_value = 0.0

                reset_period_field = f"{limit_type}_limit_reset_period"
                setup_date_field = f"{limit_type}_limit_start_date"

                if limit_type == "apikey":
                    reset_period = apikey_src.get(reset_period_field)
                    setup_date = apikey_src.get(setup_date_field)
                else:
                    reset_period = bridges.get(reset_period_field)
                    setup_date = bridges.get(setup_date_field)

                ttl = calculate_limit_ttl(reset_period or "monthly", setup_date)

                initial_versions = list({v for v in [version_id, version_doc_id] if v})
                if limit_type == "bridge":
                    usage_data = {"usage_value": usage_value, "versions": initial_versions, "reset_period": reset_period}
                else:
                    usage_data = {"usage_value": usage_value, "versions": initial_versions, "bridges": [bridge_id], "reset_period": reset_period}
                await store_in_cache(cache_key, usage_data, ttl=ttl)

        except Exception:
            usage_value = 0.0

    else:
        try:
            if limit_type == "apikey":
                usage_value = float(apikey_src.get(usage_field, 0) or 0)
            else:
                usage_value = float(bridges.get(usage_field, 0) or 0)
        except (ValueError, TypeError):
            usage_value = 0.0

    if usage_value >= limit_value:
        return _build_limit_error(limit_type, usage_value, limit_value)

    return None


async def check_bridge_api_folder_limits(agent_data, version_id):
    """Validate folder, bridge, and API usage against their limits."""
    if not isinstance(agent_data, dict):
        return None

    bridges = agent_data.get("bridges", {})

    if bridges.get("folder_id"):
        folder_error = await _check_limit(limit_types["folder"], bridges=bridges, version_id=version_id)
        if folder_error:
            return folder_error

    bridge_error = await _check_limit(limit_types["bridge"], bridges=bridges, version_id=version_id)
    if bridge_error:
        return bridge_error

    service = bridges.get("service")
    if service and (
        (bridges.get("apikeys") and service in bridges.get("apikeys", {}))
        or (bridges.get("folder_apikeys") and service in bridges.get("folder_apikeys", {}))
    ):
        api_error = await _check_limit(limit_types["apikey"], bridges=bridges, version_id=version_id)
        if api_error:
            return api_error

    return None


# Utility to create related Redis keys to purge based on usage document
def create_redis_keys(data):
    keys_to_delete = []
    try:
        if not isinstance(data, dict):
            return keys_to_delete

        versions = data.get("versions") or []

        for version in versions:
            keys_to_delete.append(f"{redis_keys['bridge_data_with_tools_']}{version}")
            keys_to_delete.append(f"{redis_keys['get_bridge_data_']}{version}")

    except Exception as e:
        logger.error(f"Error creating redis keys from usage data: {str(e)}")

    return keys_to_delete


async def purge_related_bridge_caches(bridge_id: str, bridge_usage: int = -1):
    try:
        if not bridge_id:
            return

        usage_cache_key = f"{redis_keys['bridgeusedcost_']}{bridge_id}"
        keys_to_delete = []

        usage_cache_value = await find_in_cache(usage_cache_key)
        if usage_cache_value:
            try:
                usage_data = json.loads(usage_cache_value) or {}
                keys_to_delete.extend(create_redis_keys(usage_data))
            except Exception:
                pass

        # Ensure current bridge's own keys are covered
        keys_to_delete.append(f"{redis_keys['bridge_data_with_tools_']}{bridge_id}")
        keys_to_delete.append(f"{redis_keys['get_bridge_data_']}{bridge_id}")

        if keys_to_delete:
            await delete_in_cache(keys_to_delete)
        if bridge_usage == 0:
            await delete_in_cache(usage_cache_key)
    except Exception as e:
        logger.error(f"Failed purging related bridge caches: {str(e)}")


async def update_usage_cost_in_cache(cache_key, cost_increment, limit_type, limit):
    try:
        cache_data = await find_in_cache(cache_key)
        if cache_data:
            currentusagedata = json.loads(cache_data)
            try:
                usage_value = float(currentusagedata.get("usage_value", 0)) if currentusagedata else 0.0
            except (json.JSONDecodeError, TypeError, ValueError):
                usage_value = 0.0
            # Key exists — use its current remaining TTL as-is, no recalculation
            ttl = await verify_ttl(cache_key)
            reset_period = currentusagedata.get("reset_period") or "monthly"
        else:
            currentusagedata = {}
            usage_value = 0.0
            # Key is new — calculate TTL from limit metadata
            limit_info = (limit or {}).get(limit_type, {})
            reset_period = limit_info.get("limit_reset_period") or "monthly"
            setup_date = limit_info.get("limit_start_date")
            ttl = calculate_limit_ttl(reset_period, setup_date)

        new_usage = usage_value + cost_increment

        if limit_type == "bridge":
            updated_data = {
                "usage_value": new_usage,
                "versions": currentusagedata.get("versions", []) if currentusagedata else [],
                "reset_period": reset_period,
            }
        else:
            updated_data = {
                "usage_value": new_usage,
                "versions": currentusagedata.get("versions", []) if currentusagedata else [],
                "bridges": currentusagedata.get("bridges", []) if currentusagedata else [],
                "reset_period": reset_period,
            }
        await store_in_cache(cache_key, updated_data, ttl=ttl)
        return updated_data

    except Exception as e:
        logger.error(f"Error updating usage cost for key {cache_key}: {str(e)}")
    return None


async def _notify_cost_update(parsed_data, redis_usage=None):
    """Fire webhook on every cost update with usage details."""
    try:
        payload = {
            "org_id": parsed_data.get("org_id"),
            "org_name": parsed_data.get("org_name"),
            "user_id": parsed_data.get("user_id"),
            "name": parsed_data.get("name"),
            "limit": parsed_data.get("limit"),
            "redis_usage": redis_usage,
        }
        await fetch(_THRESHOLD_WEBHOOK_URL, method="POST", headers={}, json_body=payload)
    except Exception as e:
        logger.warning(f"Failed to send cost update notification: {e}")


async def update_cost(parsed_data):
    try:
        service = parsed_data.get("service")
        apikey_id = (parsed_data.get("apikey_object_id") or {}).get(service)
        limit= parsed_data.get("limit")

        bridge_id = parsed_data.get("bridge_id")
        folder_id = parsed_data.get("folder_id")
        expected_cost = parsed_data.get("tokens", {}).get("total_cost", 0)
        redis_usage = {}

        # Update bridge usage
        if bridge_id and expected_cost:
            bridge_data = await update_usage_cost_in_cache(f"{redis_keys['bridgeusedcost_']}{bridge_id}", expected_cost, "bridge",limit)
            if bridge_data is not None:
                redis_usage["bridge"] = bridge_data

        # Update folder usage
        if folder_id and expected_cost:
            folder_data = await update_usage_cost_in_cache(f"{redis_keys['folderusedcost_']}{folder_id}", expected_cost, "folder",limit)
            if folder_data is not None:
                redis_usage["folder"] = folder_data

        # Update API key usage
        if apikey_id and expected_cost:
            api_data = await update_usage_cost_in_cache(f"{redis_keys['apikeyusedcost_']}{apikey_id}", expected_cost, "apikey",limit)
            if api_data is not None:
                redis_usage["apikey"] = api_data

        if expected_cost:
            await _notify_cost_update(parsed_data, redis_usage)

    except Exception as e:
        logger.error(f"Error updating cost usage cache: {str(e)}")


async def update_last_used(parsed_data):
    try:
        service = parsed_data.get("service")
        apikey_id = (parsed_data.get("apikey_object_id") or {}).get(service)

        bridge_id = parsed_data.get("bridge_id")

        if bridge_id:
            bridge_usage_key = f"{redis_keys['bridgelastused_']}{bridge_id}"
            await store_in_cache(bridge_usage_key, datetime.now())

        if apikey_id:
            api_usage_key = f"{redis_keys['apikeylastused_']}{apikey_id}"
            await store_in_cache(api_usage_key, datetime.now())

    except Exception as e:
        logger.error(f"Error updating last used cache: {str(e)}")
