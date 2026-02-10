from sqlalchemy import ARRAY, JSON, Boolean, Column, DateTime, Integer, String, Text
from sqlalchemy.sql import func

from models.postgres.pg_connection import Base


class system_prompt_versionings(Base):
    __tablename__ = "system_prompt_versionings"
    __table_args__ = {"extend_existing": True}

    id = Column(Integer, primary_key=True, autoincrement=True)
    created_at = Column(DateTime, nullable=False, default=func.now())
    updated_at = Column(DateTime, nullable=False, default=func.now(), onupdate=func.now())
    system_prompt = Column(Text, nullable=False)
    bridge_id = Column(String, nullable=False)
    org_id = Column(String, nullable=False)


class user_bridge_config_history(Base):
    __tablename__ = "user_bridge_config_history"
    __table_args__ = {"extend_existing": True}

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, nullable=False)
    org_id = Column(String, nullable=False)
    bridge_id = Column(String, nullable=False)
    type = Column(String, nullable=False)
    time = Column(DateTime, nullable=False, default=func.now())
    version_id = Column(String, nullable=True, default="")


class OrchestratorConversationLog(Base):
    __tablename__ = "orchestrator_conversation_logs"
    __table_args__ = {"extend_existing": True}

    id = Column(Integer, primary_key=True, autoincrement=True)
    llm_message = Column(JSON, nullable=True)  # {"bridge_id": "message"}
    user = Column(JSON, nullable=True)  # {"bridge_id": "user"}
    chatbot_message = Column(JSON, nullable=True)  # {"bridge_id": "chatbot_message"}
    updated_llm_message = Column(JSON, nullable=True)  # {"bridge_id": "updated_llm_message"}
    prompt = Column(JSON, nullable=True)  # {"bridge_id": "prompt"}
    error = Column(JSON, nullable=True)  # {"bridge_id": "error"}
    tools_call_data = Column(JSON, nullable=True, default={})  # {"bridge_id": tools_call_data}
    message_id = Column(JSON, nullable=True)  # {"bridge_id": "message_id"}
    sub_thread_id = Column(String, nullable=True)
    thread_id = Column(String, nullable=True)
    version_id = Column(JSON, nullable=True)  # {"bridge_id": "version_id"}
    bridge_id = Column(JSON, nullable=True)  # {"bridge_id": "bridge_id"}
    image_urls = Column(JSON, nullable=True, default=[])  # [{"bridge_id": ["url1", "url2"]}]
    urls = Column(JSON, nullable=True, default=[])  # [{"bridge_id": ["url1", "url2"]}]
    AiConfig = Column(JSON, nullable=True)  # {"bridge_id": AiConfig}
    fallback_model = Column(JSON, nullable=True)  # {"bridge_id": "fallback_model"}
    org_id = Column(String, nullable=True)
    service = Column(String, nullable=True)
    model = Column(JSON, nullable=True)  # {"bridge_id": "model"}
    status = Column(JSON, nullable=True, default={})  # {"bridge_id": true/false}
    tokens = Column(JSON, nullable=True)  # {"bridge_id": {"input": 120, "output": 30}}
    variables = Column(JSON, nullable=True)  # {"bridge_id": variables}
    latency = Column(JSON, nullable=True)  # {"bridge_id": latency}
    firstAttemptError = Column(JSON, nullable=True)  # {"bridge_id": "error"}
    finish_reason = Column(JSON, nullable=True)  # {"bridge_id": "finish_reason"}
    created_at = Column(DateTime, nullable=False, default=func.now())
    updated_at = Column(DateTime, nullable=False, default=func.now(), onupdate=func.now())
    agents_path = Column(ARRAY(String), nullable=True, default=[])


class ConversationLog(Base):
    __tablename__ = "conversation_logs"
    __table_args__ = {"extend_existing": True}

    id = Column(Integer, primary_key=True, autoincrement=True)
    llm_message = Column(Text, nullable=True)
    user = Column(Text, nullable=True)
    chatbot_message = Column(Text, nullable=True)
    updated_llm_message = Column(Text, nullable=True)
    error = Column(Text, nullable=True)
    user_feedback = Column(Integer, nullable=True, default=0)
    tools_call_data = Column(JSON, nullable=True, default=[])
    message_id = Column(String, nullable=True)
    sub_thread_id = Column(String, nullable=True)
    thread_id = Column(String, nullable=True)
    version_id = Column(String, nullable=True)
    bridge_id = Column(String, nullable=True)
    user_urls = Column(JSON, nullable=True, default=[])
    llm_urls = Column(JSON, nullable=True, default=[])
    AiConfig = Column(JSON, nullable=True)
    fallback_model = Column(JSON, nullable=True)
    org_id = Column(String, nullable=True)
    service = Column(String, nullable=True)
    model = Column(String, nullable=True)
    status = Column(Boolean, nullable=True, default=False)
    tokens = Column(JSON, nullable=True)
    variables = Column(JSON, nullable=True)
    latency = Column(JSON, nullable=True)
    firstAttemptError = Column(Text, nullable=True)
    finish_reason = Column(String, nullable=True)
    parent_id = Column(String, nullable=True)
    child_id = Column(String, nullable=True)
    prompt = Column(JSON, nullable=True)
    created_at = Column(DateTime, nullable=False, default=func.now())
    updated_at = Column(DateTime, nullable=False, default=func.now(), onupdate=func.now())
