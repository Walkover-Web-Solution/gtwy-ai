from fastapi import APIRouter, HTTPException

from db.session_db_service import delete_session, get_session, list_sessions

router = APIRouter(prefix="/sessions", tags=["Sessions"])


@router.get("")
async def list_sessions_endpoint(agent_id: str = None, org_id: str = None):
    """List sessions, optionally filtered by agent_id or org_id."""
    try:
        sessions = await list_sessions(agent_id=agent_id, org_id=org_id)
        # Return lightweight list (exclude full state to keep response small)
        data = [
            {
                "session_id": s["session_id"],
                "agent_id": s.get("agent_id"),
                "thread_id": s.get("thread_id"),
                "goal": s.get("goal", ""),
                "status": s.get("status", "unknown"),
                "created_at": s.get("created_at"),
                "updated_at": s.get("updated_at"),
            }
            for s in sessions
        ]
        return {"success": True, "data": data, "count": len(data)}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/{session_id}")
async def get_session_endpoint(session_id: str):
    """Get a specific session with messages."""
    session = await get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail=f"Session '{session_id}' not found.")
    return {"success": True, "data": session}


@router.delete("/{session_id}")
async def delete_session_endpoint(session_id: str):
    """Delete a session."""
    deleted = await delete_session(session_id)
    if not deleted:
        raise HTTPException(status_code=404, detail=f"Session '{session_id}' not found.")
    return {"success": True, "message": f"Session '{session_id}' deleted."}
