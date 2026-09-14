"""Loader for the billing_plans collection.

A plan is an ALLOWLIST: it names the services whose models wallet credits may be
spent on, optionally narrowed to specific models per service.

Authored (Mongo) shape:

    {"plan_code": "free", "display_name": "Free", "status": 1, "credit_grant": 100,
     "services": {"neev_cloud": "*", "open_router": ["a:free", "b:free"]}}

    {"plan_code": "paid", "display_name": "Pro", "status": 1, "credit_grant": 0,
     "services": "*"}

Everything is normalised HERE, at load time, not on the request path:
  * "*" becomes the ALL_MODELS sentinel object, never the string — so a model
    literally named "*" can never match by accident;
  * model lists become frozensets, so membership is one hash instead of an O(k)
    list scan per model per agent.

Service keys and model names are compared verbatim, case-sensitively, against
`cfg["service"]` and `configuration["model"]` — do NOT case-fold anywhere.
"""

from globals import logger
from models.mongo_connection import db
from src.services.utils.time import with_timeout

planConfigModel = db["billing_plans"]

_WILDCARD = "*"


class _AllModels:
    """Sentinel: every model of this service (or every service, for a plan)."""

    __slots__ = ()

    def __repr__(self):
        return "<ALL_MODELS>"


ALL_MODELS = _AllModels()


def _normalize_services(raw, plan_code):
    """Return (all_services, {service: frozenset | ALL_MODELS}), or None.

    None means the plan normalises to "nothing allowed" — the caller rejects the
    whole load rather than installing it. Malformed values deny the service they
    appear on rather than widening it: fail-closed per key.
    """
    if raw == _WILDCARD:
        return True, {}
    if not isinstance(raw, dict) or not raw:
        return None

    services = {}
    for service, models in raw.items():
        if not service or not isinstance(service, str):
            logger.error(f"[plans] plan '{plan_code}': ignoring non-string service key {service!r}")
            continue
        if models == _WILDCARD:
            services[service] = ALL_MODELS
        elif isinstance(models, (list, tuple, set)):
            names = frozenset(m for m in models if isinstance(m, str) and m)
            if not names:
                logger.error(f"[plans] plan '{plan_code}': service '{service}' lists no models — denying it")
                continue
            services[service] = names
        else:
            logger.error(
                f"[plans] plan '{plan_code}': service '{service}' has unsupported value "
                f"{type(models).__name__} — denying it"
            )
    return (False, services) if services else None


async def get_plan_configs() -> dict:
    """Load and normalize billing_plans.

    Returns {} on ANY problem — an unreadable collection, or a plan that allows
    nothing. {} is what makes init_plan_registry's non-empty guard keep the
    last-good registry instead of installing a definition that would 400 every
    wallet request for that plan's orgs.
    """
    try:
        docs = await with_timeout(planConfigModel.find({"status": 1}, {"_id": 0}).to_list(length=None))
    except Exception as error:
        logger.error(f"Error fetching billing plans: {error}")
        return {}

    registry = {}
    for doc in docs:
        plan_code = doc.get("plan_code")
        if not plan_code or not isinstance(plan_code, str):
            logger.error(f"[plans] skipping billing_plans doc with no usable plan_code: {doc!r}")
            continue
        normalized = _normalize_services(doc.get("services"), plan_code)
        if normalized is None:
            # An empty allowlist is almost always an admin/UI slip, and for the
            # free plan it is a full outage. Reject the ENTIRE load so the
            # previously loaded registry survives.
            logger.error(
                f"[plans] plan '{plan_code}' allows nothing — rejecting the whole billing_plans "
                "load and keeping the previous registry"
            )
            return {}
        all_services, services = normalized
        registry[plan_code] = {
            "plan_code": plan_code,
            "display_name": doc.get("display_name") or plan_code,
            "all_services": all_services,
            "services": services,
        }
    return registry
