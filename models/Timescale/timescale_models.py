from sqlalchemy import Boolean, Column, DateTime, Float, Integer, String
from sqlalchemy.sql import func

from models.Timescale.connections import Base


class Metrics_model(Base):
    __tablename__ = "metrics_raw_data"
    __table_args__ = {"extend_existing": True}

    id = Column(Integer, primary_key=True, autoincrement=True)
    org_id = Column(String)
    bridge_id = Column(String, nullable=True)
    version_id = Column(String, nullable=True)
    thread_id = Column(String)
    model = Column(String)
    service = Column(String)
    input_tokens = Column(Float)
    output_tokens = Column(Float)
    total_tokens = Column(Float)
    apikey_id = Column(String, nullable=True)
    created_at = Column(DateTime, default=func.now())
    latency = Column(Float)
    success = Column(Boolean)
    cost = Column(Float)
    time_zone = Column(String)
