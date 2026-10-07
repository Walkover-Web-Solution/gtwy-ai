"""DB-driven web search settings.

Whether a request gets provider-native web search, and what that looks like on the wire, comes
from the database instead of per-handler code:

- ``modelconfigurations.validationConfig.inbuilt_tools.web_search`` (bool): the model supports it.
- ``services.web_search_tool``: how the provider expects it, in one of these shapes:

  - ``{"unfiltered": {...}, "filtered": {...}, "max_domains": 5}``: tool entries for the tools list.
    ``filtered`` is used when the agent sets allowed domains; every ``"allowed_domains": null`` in it
    is a placeholder that receives the domains (capped at the optional ``max_domains``).
  - ``{...}``: a single tool entry used as-is (e.g. Gemini ``{"google_search": {}}``).
  - ``{"extra_body": {...}}``: merged into extra_body instead of adding a tool (OpenRouter's plugin).

  Any shape can also carry ``"endpoint": "/responses"``: the path under the service's ``base_url`` that
  web search requests must be sent to, for providers that only offer it there (Grok, MiniMax).

Handlers keep only provider mechanics (e.g. Gemini's tool-combination rules, routing Grok/MiniMax to a
Responses endpoint); the tool payload and the per-model switch are read here.
"""

import copy

from src.configs.model_configuration import model_config_document
from src.configs.service_registry import base_url, web_search_tool_config

WEB_SEARCH = "web_search"

# Settings stored next to the tool entry; never part of the tool itself
_META_KEYS = ("max_domains", "endpoint")


def web_search_requested(built_in_tools):
    return bool(built_in_tools) and WEB_SEARCH in built_in_tools


def model_supports_web_search(service, model):
    """True only when the model's validationConfig.inbuilt_tools.web_search flag is set in the DB."""
    model_document = model_config_document.get(service, {}).get(model, {}) or {}
    inbuilt_tools = (model_document.get("validationConfig") or {}).get("inbuilt_tools") or {}
    return inbuilt_tools.get(WEB_SEARCH) is True


def use_web_search(service, model, built_in_tools):
    """The agent asked for web search and the model supports it."""
    return web_search_requested(built_in_tools) and model_supports_web_search(service, model)


def _fill_domain_placeholders(node, domains):
    """Replace every ``"allowed_domains": null`` placeholder in a filtered tool with the domains."""
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "allowed_domains" and value is None:
                node[key] = domains
            else:
                _fill_domain_placeholders(value, domains)
    elif isinstance(node, list):
        for item in node:
            _fill_domain_placeholders(item, domains)
    return node


def build_web_search_tool(service, domain_filters=None):
    """The provider's web search tool entry from services.web_search_tool, with allowed domains applied."""
    config = web_search_tool_config(service)
    if not config or "extra_body" in config:
        return None
    if "unfiltered" in config or "filtered" in config:
        domains = domain_filters if isinstance(domain_filters, list) and domain_filters else None
        if domains and config.get("filtered"):
            max_domains = config.get("max_domains")
            return _fill_domain_placeholders(copy.deepcopy(config["filtered"]), domains[:max_domains] if max_domains else domains)
        return copy.deepcopy(config.get("unfiltered")) if config.get("unfiltered") else None
    return copy.deepcopy({key: value for key, value in config.items() if key not in _META_KEYS})


def _tool_types(service):
    config = web_search_tool_config(service)
    if "unfiltered" in config or "filtered" in config:
        entries = [config.get("unfiltered"), config.get("filtered")]
    else:
        entries = [config]
    return {entry.get("type") for entry in entries if isinstance(entry, dict) and entry.get("type")}


def has_web_search_tool(service, configuration):
    """The request carries this service's web search tool (matched by the tool types stored in the DB)."""
    types = _tool_types(service)
    return bool(types) and any(
        isinstance(tool, dict) and tool.get("type") in types for tool in configuration.get("tools") or []
    )


def web_search_endpoint(service):
    """Full URL web search requests must go to (service base_url + web_search_tool.endpoint), or None."""
    endpoint = web_search_tool_config(service).get("endpoint")
    if not endpoint:
        return None
    if endpoint.startswith("http"):
        return endpoint
    root = base_url(service)
    return f"{root.rstrip('/')}/{endpoint.lstrip('/')}" if root else None


def web_search_extra_body(service):
    """extra_body additions for providers that enable web search outside the tools list (OpenRouter)."""
    extra_body = web_search_tool_config(service).get("extra_body")
    return copy.deepcopy(extra_body) if extra_body else None


def merge_extra_body(current, addition):
    """Merge web search extra_body into an existing one; list values (e.g. plugins) are appended."""
    merged = dict(current or {})
    for key, value in (addition or {}).items():
        if isinstance(value, list) and isinstance(merged.get(key), list):
            merged[key] = merged[key] + value
        else:
            merged[key] = value
    return merged
