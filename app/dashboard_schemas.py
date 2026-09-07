from datetime import datetime

from pydantic import BaseModel, Field


class DashboardPeriod(BaseModel):
    start_at: datetime
    end_at: datetime


class DashboardKpis(BaseModel):
    user_signup_count: int
    total_requests: int
    success_count: int
    failure_count: int
    error_rate_percent: float
    average_latency_ms: int


class HourlyRequestStat(BaseModel):
    hour: datetime
    request_count: int
    success_count: int
    failure_count: int


class EndpointUsageStat(BaseModel):
    endpoint: str
    request_count: int
    unique_user_count: int
    success_count: int
    failure_count: int
    average_latency_ms: int
    error_rate_percent: float


class LlmSummaryStat(BaseModel):
    model: str
    request_count: int
    success_count: int
    failure_count: int
    error_rate_percent: float
    average_latency_ms: int
    timeout_count: int
    gemini_error_count: int
    empty_response_count: int


class DashboardSummaryResponse(BaseModel):
    period: DashboardPeriod
    kpis: DashboardKpis
    hourly_requests: list[HourlyRequestStat]
    endpoint_usage: list[EndpointUsageStat]
    llm_summary: list[LlmSummaryStat]


class ErrorLogItem(BaseModel):
    occurred_at: datetime
    request_id: str
    method: str
    endpoint: str
    status_code: int | None
    latency_ms: int | None
    error_type: str | None
    user_id: str | None
    trip_id: str | None
    model: str | None


class ErrorLogResponse(BaseModel):
    items: list[ErrorLogItem]
    count: int = Field(ge=0)
