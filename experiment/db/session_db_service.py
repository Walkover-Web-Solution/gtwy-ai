import uuid

from db.json_store import now_str, store_delete, store_find_one, store_get, store_list, store_put, store_update


def _generate_session_id() -> str:
    return f"sess_{uuid.uuid4().hex[:12]}"


async def create_session(session_data: dict) -> dict:
    """Create a new agent session."""
    now = now_str()
    session = {
        "session_id": _generate_session_id(),
        "agent_id": session_data["agent_id"],
        "org_id": session_data.get("org_id", "default"),
        "thread_id": session_data.get("thread_id", str(uuid.uuid4())),
        "goal": session_data.get("goal", ""),
        "state": session_data.get("state", {}),
        "messages": session_data.get("messages", []),
        "a2a_calls": [],
        "status": "active",
        "created_at": now,
        "updated_at": now,
    }
    return await store_put("sessions", session["session_id"], session)


async def get_session(session_id: str) -> dict | None:
    """Get a session by session_id."""
    return await store_get("sessions", session_id)


async def get_session_by_thread(thread_id: str) -> dict | None:
    """Get a session by thread_id."""
    return await store_find_one("sessions", lambda s: s.get("thread_id") == thread_id)


async def list_sessions(agent_id: str = None, org_id: str = None) -> list[dict]:
    """List sessions, optionally filtered by agent_id or org_id."""
    def _filter(s):
        if agent_id and s.get("agent_id") != agent_id:
            return False
        if org_id and s.get("org_id") != org_id:
            return False
        return True
    sessions = await store_list("sessions", _filter)
    return sorted(sessions, key=lambda s: s.get("created_at", ""), reverse=True)


async def update_session(session_id: str, updates: dict) -> dict | None:
    """Update a session's state or status."""
    updates["updated_at"] = now_str()
    updates.pop("session_id", None)
    updates.pop("created_at", None)
    return await store_update("sessions", session_id, updates)


async def append_message(session_id: str, message: dict) -> bool:
    """Append a message to the session's message history."""
    session = await store_get("sessions", session_id)
    if not session:
        return False
    session.setdefault("messages", []).append(message)
    await store_put("sessions", session_id, session)
    return True


async def append_a2a_call(session_id: str, a2a_call: dict) -> bool:
    """Append an A2A call log entry to the session."""
    session = await store_get("sessions", session_id)
    if not session:
        return False
    a2a_call["timestamp"] = now_str()
    session.setdefault("a2a_calls", []).append(a2a_call)
    await store_put("sessions", session_id, session)
    return True


async def delete_session(session_id: str) -> bool:
    """Delete a session."""
    return await store_delete("sessions", session_id)
