"""DB-driven billing-plan registry.

A plan is an ALLOWLIST of services whose models wallet credits may be spent on,
optionally narrowed to specific models per service. `"*"` for a service means all
its models (including ones added later); `services: "*"` means everything.

Editable at runtime: the billing_plans collection is the source of truth and this
registry refreshes from a Mongo change stream, so changing what a plan includes
needs no deploy and no migration. That is the whole point — it replaces the
`free_tier` boolean, which was spread across hundreds of modelconfigurations
documents with no reachable admin API.

POLICY, stated once and implemented once (in plan_allows):

  * plan resolution       fail-CLOSED. An org we cannot classify gets
                          DEFAULT_PLAN_CODE (billing_utils.get_org_plan).
  * allowlist evaluation  fail-CLOSED. Unknown plan code, unlisted service and
                          unlisted model all DENY. There is deliberately no
                          "not the free plan, therefore unrestricted" branch —
                          that was the old fail-open hole in
                          fallback_allowed_on_plan.
  * registry availability fail-OPEN, and this is the ONLY fail-open door. An
                          empty registry allows everything, because denying
                          would 400 every wallet request on the platform. Same
                          trade-off platform_apikey_document makes.

Unlike model_configuration (which imports baseService.utils), this module pulls
only config/globals/mongo/pymongo plus its loader, so there is no import cycle
and billing_utils can import it at the top level. The one thing that MUST stay
lazy is send_message inside the listener.
"""

import asyncio
import time

from pymongo.errors import OperationFailure, PyMongoError

from config import Config
from globals import logger
from models.mongo_connection import db
from src.services.utils.load_plan_configs import ALL_MODELS, get_plan_configs

plan_config_model = db["billing_plans"]

# NEVER rebind this dict — importers hold the reference. .clear() + .update(),
# same contract as model_config_document and service_registry_document.
plan_registry_document: dict = {}

# The most restrictive plan we ship. Orgs we cannot classify land here.
DEFAULT_PLAN_CODE = "free"

# The empty-registry warning is evaluated on the hot path; unthrottled it would
# fire once per agent per request and turn a degraded state into a log incident.
_EMPTY_WARN_INTERVAL = 60.0
_last_empty_warn = 0.0


def _registry_unavailable() -> bool:
    """True when no plan definition has ever loaded (the fail-open condition)."""
    if plan_registry_document:
        return False
    global _last_empty_warn
    now = time.monotonic()
    if now - _last_empty_warn > _EMPTY_WARN_INTERVAL:
        _last_empty_warn = now
        logger.error("[plans] plan registry is EMPTY — plan enforcement is DISABLED (failing open)")
    return True


def registry_is_loaded() -> bool:
    return bool(plan_registry_document)


def get_plan(plan_code) -> dict | None:
    return plan_registry_document.get(plan_code) if plan_code else None


def plan_exists(plan_code) -> bool:
    return bool(plan_code) and plan_code in plan_registry_document


def plan_display_name(plan_code) -> str:
    """Human label for error messages. Never used for matching."""
    plan = get_plan(plan_code)
    return plan["display_name"] if plan else (plan_code or DEFAULT_PLAN_CODE)


def allowed_models_for(plan_code, service):
    """The plan's model allowlist for ONE service.

    Returns ALL_MODELS, a frozenset of model names, or None when the plan does
    not include that service at all. Callers that test many models against one
    service (the auto-router) should hoist this out of their model loop instead
    of calling plan_allows per model.
    """
    if _registry_unavailable():
        return ALL_MODELS
    plan = get_plan(plan_code)
    if plan is None:
        return None
    if plan["all_services"]:
        return ALL_MODELS
    return plan["services"].get(service)


def plan_allows(plan_code, service, model) -> bool:
    """May this plan spend wallet credits on this service+model?

    The single decision point for the policy in the module docstring.
    """
    if _registry_unavailable():
        return True
    if not service or not model:
        return False
    allowed = allowed_models_for(plan_code, service)
    if allowed is None:
        return False
    return allowed is ALL_MODELS or model in allowed


async def init_plan_registry():
    """Initialize or refresh the plan registry.

    Non-empty guard, same as init_platform_apikeys: a transient DB error or a
    rejected load must not strip the registry, because an empty registry
    disables plan enforcement for the whole platform.
    """
    try:
        new_document = await get_plan_configs()
        if new_document or not plan_registry_document:
            plan_registry_document.clear()
            plan_registry_document.update(new_document)
            logger.info(
                f"Billing plans refreshed successfully ({len(plan_registry_document)} plans: "
                f"{sorted(plan_registry_document)})."
            )
        else:
            logger.error("[plans] billing_plans load returned nothing — keeping the previous registry")
        if not plan_registry_document:
            logger.error(
                "[plans] billing_plans is EMPTY — plan enforcement is DISABLED. Seed it "
                "(Node: migrations/mongo/*-seed_billing_plans.js)."
            )
    except Exception as e:
        logger.error(f"Error refreshing billing plans: {e}")


async def _async_change_listener():
    from src.services.commonServices.baseService.utils import send_message  # lazy: avoids import cycle

    pipeline = [{"$match": {"operationType": {"$in": ["insert", "update", "replace", "delete"]}}}]
    async with plan_config_model.watch(pipeline, full_document="updateLookup") as stream:
        logger.info("MongoDB change stream is now listening for billing plan changes.")
        async for change in stream:
            logger.info(f"Change detected in billing plans: {change['operationType']}")
            await init_plan_registry()
            await send_message(
                cred={"apikey": Config.RTLAYER_AUTH, "ttl": 1, "channel": "global_model_updates"},
                data={
                    "event": "billing_plans_updated",
                    "operation": change["operationType"],
                    "plan_code": change.get("fullDocument", {}).get("plan_code"),
                    "timestamp": str(change.get("clusterTime", "")),
                },
            )
            logger.info("Billing plan change detected and sent to RTLayer successfully.")


async def background_listen_for_plan_changes():
    """Background task: change-stream listener with a retry/reconnect loop."""
    while True:
        try:
            await _async_change_listener()
        except (OperationFailure, PyMongoError) as e:
            logger.error(f"Billing plan change stream error: {e}. Reconnecting in 5 seconds...")
            await asyncio.sleep(5)
        except Exception as e:
            logger.error(f"Unexpected error in billing plan listener: {e}. Restarting in 10 seconds...")
            await asyncio.sleep(10)
