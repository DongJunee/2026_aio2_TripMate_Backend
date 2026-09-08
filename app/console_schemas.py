from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class ConsoleUserItem(BaseModel):
    id: str
    email: str
    username: str
    created_at: datetime | None = None
    last_active_at: datetime | None = None
    trip_count: int = Field(ge=0)
    request_count: int = Field(ge=0)
    activity_count: int = Field(ge=0)


class ConsoleUserListResponse(BaseModel):
    items: list[ConsoleUserItem]
    count: int = Field(ge=0)
    total: int = Field(ge=0)


class ConsoleUserDetail(ConsoleUserItem):
    trips: list[dict[str, Any]]
    recent_requests: list[dict[str, Any]]
    recent_activities: list[dict[str, Any]]


class FeedbackBreakdown(BaseModel):
    label: str
    count: int = Field(ge=0)


class ConsoleFeedbackSummary(BaseModel):
    feedback_count: int = Field(ge=0)
    positive_count: int = Field(ge=0)
    negative_count: int = Field(ge=0)
    pace_count: int = Field(ge=0)
    feedback_breakdown: list[FeedbackBreakdown]
    pace_breakdown: list[FeedbackBreakdown]


class SystemStatusItem(BaseModel):
    service: str
    status: str
    request_count: int = Field(ge=0)
    failure_rate_percent: float = Field(ge=0)
    p95_latency_ms: int = Field(ge=0)


class ConsoleSystemStatusResponse(BaseModel):
    window_start: datetime
    window_end: datetime
    total_requests: int = Field(ge=0)
    failure_count: int = Field(ge=0)
    error_rate_percent: float = Field(ge=0)
    services: list[SystemStatusItem]
