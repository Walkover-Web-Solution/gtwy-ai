import json
import urllib.request
from poc.config import GTWY_BASE_URL, GTWY_PAUTH_KEY


def call_gtwy(user_message: str, bridge_id: str, thread_id: str = None, variables: dict = None) -> str:
    url = f"{GTWY_BASE_URL}/api/v2/model/chat/completion"
    body = {"user": user_message, "bridge_id": bridge_id}
    if thread_id:
        body["thread_id"] = thread_id
    if variables:
        body["variables"] = variables

    payload = json.dumps(body).encode()
    req = urllib.request.Request(
        url,
        data=payload,
        headers={
            "pauthkey": GTWY_PAUTH_KEY,
            "Content-Type": "application/json",
            "User-Agent": "Mozilla/5.0 (compatible; gtwy-poc/1.0)",
            "Accept": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        raw = resp.read().decode()

    return _parse_sse(raw)


def _parse_sse(raw: str) -> str:
    """Parse SSE stream and return the full assembled content."""
    content_parts = []
    final_content = None

    for line in raw.splitlines():
        if not line.startswith("data:"):
            continue
        data_str = line[len("data:"):].strip()
        if not data_str:
            continue
        try:
            event = json.loads(data_str)
        except json.JSONDecodeError:
            continue

        ev = event.get("event")
        if ev == "delta":
            content_parts.append(event.get("content", ""))
        elif ev == "done":
            final_content = (
                event.get("response", {}).get("data", {}).get("content")
                or event.get("content")
            )

    if final_content:
        return final_content

    assembled = "".join(content_parts)
    if assembled:
        return assembled

    raise RuntimeError(f"No content in GTWY response. Raw: {raw[:300]}")
