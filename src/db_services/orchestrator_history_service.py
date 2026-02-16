from src.services.utils.logger import logger


# Global object to store orchestrator data by bridge_id during execution
class OrchestratorDataCollector:
    """Global collector for orchestrator data during execution"""

    def __init__(self):
        self._data = {}

    def initialize_session(self, thread_id: str, org_id: str, orchestrator_id: str):
        """Initialize a new orchestrator session"""
        if thread_id not in self._data:
            self._data[thread_id] = {
                "org_id": org_id,
                "thread_id": thread_id,
                "sub_thread_id": thread_id,
                "orchestrator_id": orchestrator_id,
                "model_name": {},
                "user": {},
                "response": {},
                "tool_call_data": {},
                "latency": {},
                "tokens": {},
                "error": {},
                "variables": {},
                "user_urls": {},
                "llm_urls": {},
                "ai_config": {},
            }

    def add_bridge_data(self, thread_id: str, bridge_id: str, data: dict):
        """Add data for a specific bridge_id"""
        if thread_id not in self._data:
            logger.warning(f"Thread {thread_id} not initialized in orchestrator collector")
            return

        session_data = self._data[thread_id]

        # Store data by bridge_id
        if "model_name" in data:
            session_data["model_name"][bridge_id] = data["model_name"]

        if "user" in data:
            session_data["user"][bridge_id] = data["user"]

        if "response" in data:
            session_data["response"][bridge_id] = data["response"]

        if "tool_call_data" in data:
            session_data["tool_call_data"][bridge_id] = data["tool_call_data"]

        if "latency" in data:
            session_data["latency"][bridge_id] = data["latency"]

        if "tokens" in data:
            session_data["tokens"][bridge_id] = data["tokens"]

        if "error" in data:
            session_data["error"][bridge_id] = data["error"]

        if "variables" in data:
            session_data["variables"][bridge_id] = data["variables"]

        if "user_urls" in data:
            session_data["user_urls"][bridge_id] = data["user_urls"]

        if "llm_urls" in data:
            session_data["llm_urls"][bridge_id] = data["llm_urls"]

        if "ai_config" in data:
            session_data["ai_config"][bridge_id] = data["ai_config"]

    def get_session_data(self, thread_id: str) -> dict | None:
        """Get all collected data for a thread"""
        return self._data.get(thread_id)

    def clear_session(self, thread_id: str):
        """Clear data for a specific thread"""
        if thread_id in self._data:
            del self._data[thread_id]

    def get_all_sessions(self) -> dict:
        """Get all active sessions (for debugging)"""
        return self._data


# Global instance
orchestrator_collector = OrchestratorDataCollector()
