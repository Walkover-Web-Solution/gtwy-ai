from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field


class UsageLimitConfig(BaseModel):
    """MongoDB document for usage limit configuration on bridges."""

    org_id: str = Field(description="Organization ID")
    bridge_id: str = Field(description="Bridge ID")

    bridge_limit: float = Field(default=0.0, description="Bridge monthly limit in USD")
    bridge_reset_period: str = Field(
        default="monthly", description="Reset period: daily, weekly, monthly"
    )
    bridge_start_date: datetime = Field(
        default_factory=datetime.utcnow, description="When this period started"
    )
    bridge_hard_stop: bool = Field(
        default=True, description="True=block, False=warn only"
    )

    folder_limit: float = Field(default=0.0, description="Folder monthly limit in USD")
    folder_reset_period: str = Field(default="monthly")
    folder_start_date: datetime = Field(default_factory=datetime.utcnow)
    folder_hard_stop: bool = Field(default=True)

    apikey_limit: float = Field(default=0.0, description="API key monthly limit in USD")
    apikey_reset_period: str = Field(default="monthly")
    apikey_start_date: datetime = Field(default_factory=datetime.utcnow)
    apikey_hard_stop: bool = Field(default=True)

    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)

    class Config:
        json_schema_extra = {
            "example": {
                "org_id": "org_123",
                "bridge_id": "bridge_456",
                "bridge_limit": 100.0,
                "bridge_reset_period": "monthly",
                "bridge_hard_stop": True,
                "folder_limit": 500.0,
                "folder_reset_period": "monthly",
                "folder_hard_stop": False,
                "apikey_limit": 1000.0,
                "apikey_reset_period": "monthly",
                "apikey_hard_stop": True,
            }
        }
