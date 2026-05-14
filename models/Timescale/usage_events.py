from datetime import datetime

from sqlalchemy import Column, DateTime, Float, Integer, String, Text, UniqueConstraint, create_engine
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker

Base = declarative_base()


class UsageEventRecord(Base):
    __tablename__ = "usage_events"

    id = Column(Integer, primary_key=True, autoincrement=True)
    request_id = Column(String(36), nullable=False, unique=True, index=True)
    org_id = Column(String(255), nullable=False, index=True)
    bridge_id = Column(String(255), nullable=False, index=True)
    folder_id = Column(String(255), nullable=True, index=True)
    apikey_id = Column(String(255), nullable=True, index=True)
    service = Column(String(100), nullable=False, index=True)
    model = Column(String(255), nullable=False)
    tokens_in = Column(Integer, nullable=False)
    tokens_out = Column(Integer, nullable=False)
    cost_usd = Column(Float, nullable=False)
    status = Column(String(50), nullable=False, default="success")
    timestamp = Column(DateTime, nullable=False, default=datetime.utcnow, index=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)

    __table_args__ = (
        UniqueConstraint("request_id", name="uq_request_id"),
    )


class DailyUsageAggregate(Base):
    __tablename__ = "daily_usage_aggregate"

    id = Column(Integer, primary_key=True, autoincrement=True)
    date = Column(String(10), nullable=False, index=True)
    org_id = Column(String(255), nullable=False, index=True)
    bridge_id = Column(String(255), nullable=True, index=True)
    folder_id = Column(String(255), nullable=True, index=True)
    apikey_id = Column(String(255), nullable=True, index=True)
    service = Column(String(100), nullable=True)
    total_cost = Column(Float, nullable=False, default=0.0)
    total_tokens_in = Column(Integer, nullable=False, default=0)
    total_tokens_out = Column(Integer, nullable=False, default=0)
    request_count = Column(Integer, nullable=False, default=0)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow)
