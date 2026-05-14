from datetime import datetime
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


class LimitResetPeriod(str, Enum):
    MONTHLY = "monthly"
    WEEKLY = "weekly"
    DAILY = "daily"


class UsageLimitConfig(BaseModel):
    limit: float = Field(default=0.0, description="Dollar amount limit")
    reset_period: LimitResetPeriod = Field(default=LimitResetPeriod.MONTHLY)
    start_date: datetime = Field(default_factory=datetime.utcnow)
    hard_stop: bool = Field(default=True, description="True=block, False=warn only")


class UsageEvent(BaseModel):
    request_id: str = Field(description="Unique UUID for this request")
    org_id: str
    bridge_id: str
    folder_id: Optional[str] = None
    apikey_id: Optional[str] = None
    service: str = Field(description="e.g., 'openai', 'anthropic'")
    model: str
    tokens_in: int
    tokens_out: int
    cost_usd: float
    status: str = Field(default="success", description="'success', 'error', 'rejected'")
    timestamp: datetime = Field(default_factory=datetime.utcnow)
    reservation_cost: Optional[float] = Field(default=None, description="Pre-charge estimate")
    actual_cost: Optional[float] = Field(default=None, description="Real cost from provider")


class UsageCheckRequest(BaseModel):
    bridge_id: str
    folder_id: Optional[str] = None
    service: str
    apikey_id: Optional[str] = None
    estimated_cost: float = Field(description="Worst-case cost estimate")


class UsageCheckResponse(BaseModel):
    allowed: bool
    reason: Optional[str] = None
    limit_type: Optional[str] = None
    current_usage: Optional[float] = None
    limit_value: Optional[float] = None
