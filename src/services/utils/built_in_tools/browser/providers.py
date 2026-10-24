"""The hosted browser services an agent can run Gtwy_Browser on, and which one it picked.

An agent picks its service with ``gtwy_browser_settings.provider``; without one it uses
DEFAULT_PROVIDER. Every service has one client module with the same functions and the same
browser view (``provider_base.py``), so the rest of the tool never branches on the service.
A service is usable only when its API key is set on this server; picking one that is not set
up is reported to the model, never silently replaced by another service, because logins are
saved per service.
"""

from . import anchor_client, browser_use_client, browserbase_client, kernel_client, steel_cloud_client

CLIENTS = {
    client.NAME: client
    for client in (browser_use_client, browserbase_client, steel_cloud_client, kernel_client, anchor_client)
}
DEFAULT_PROVIDER = browser_use_client.NAME

# Built-in tools an agent can connect on the UI, one per service. The model always sees one
# Gtwy_Browser tool; connecting one of these picks its service. Plain Gtwy_Browser keeps
# gtwy_browser_settings.provider (Browser Use by default).
SERVICE_TOOLS = {
    "Gtwy_Browser_Browserbase": browserbase_client.NAME,
    "Gtwy_Browser_Steel": steel_cloud_client.NAME,
    "Gtwy_Browser_Kernel": kernel_client.NAME,
    "Gtwy_Browser_Anchor": anchor_client.NAME,
}


class ProviderUnavailable(Exception):
    """The agent's browser service is unknown or not set up on this server."""


def get(name: str | None):
    """The client of one service; records written before services existed are Browser Use."""
    return CLIENTS.get(name or DEFAULT_PROVIDER)


def label(name: str | None) -> str:
    client = get(name)
    return client.LABEL if client else str(name)


def chosen(settings: dict | None) -> str:
    value = (settings or {}).get("provider")
    return str(value).strip().lower() if value else DEFAULT_PROVIDER


def resolve(settings: dict | None):
    """(name, client) of the service the agent picked. Raises ProviderUnavailable with a message
    the model can pass on when the name is unknown or its key is not set on this server."""
    name = chosen(settings)
    client = CLIENTS.get(name)
    if client is None:
        raise ProviderUnavailable(
            f"unknown browser service '{name}' in this agent's browser settings; valid services: {', '.join(CLIENTS)}"
        )
    if not client.is_configured():
        raise ProviderUnavailable(
            f"the {client.LABEL} browser service is not set up on this server yet. Tell the user "
            "to pick another browser service for this agent, or to ask the gtwy team to enable it"
        )
    return name, client


def fold_service_tools(built_in_tools: list, browser_tool: str) -> tuple[list, str | None]:
    """(built_in_tools with every service tool replaced by ``browser_tool`` once, the service of
    the first service tool or None). An agent runs one browser per conversation, so when several
    browser tools are connected the first service tool picks it."""
    folded, service = [], None
    for name in built_in_tools or []:
        if name in SERVICE_TOOLS:
            service = service or SERVICE_TOOLS[name]
            name = browser_tool
        if name not in folded:
            folded.append(name)
    return folded, service


def configured() -> list:
    return [client for client in CLIENTS.values() if client.is_configured()]


def any_configured() -> bool:
    return any(client.is_configured() for client in CLIENTS.values())


def catalog() -> list[dict]:
    """Every service for the UI's picker: id, label, its built-in tool, whether it is set up, and
    the default (connected as plain Gtwy_Browser)."""
    tool_of = {service: tool for tool, service in SERVICE_TOOLS.items()}
    return [
        {
            "id": name,
            "label": client.LABEL,
            "tool": tool_of.get(name, "Gtwy_Browser"),
            "configured": client.is_configured(),
            "default": name == DEFAULT_PROVIDER,
        }
        for name, client in CLIENTS.items()
    ]
