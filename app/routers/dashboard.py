from __future__ import annotations

import os
import secrets
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, status
from fastapi.security import HTTPAuthorizationCredentials

from app.dashboard_schemas import (
    DashboardKpis,
    DashboardPeriod,
    DashboardSummaryResponse,
    EndpointUsageStat,
    ErrorLogItem,
    ErrorLogResponse,
    HourlyRequestStat,
    LlmSummaryStat,
)
from app.dashboard_auth import is_dashboard_admin
from app.db import get_service_client
from app.deps import bearer_scheme, get_current_user

router = APIRouter(prefix="/admin/dashboard", tags=["admin-dashboard"])
KST = ZoneInfo("Asia/Seoul")
MAX_PERIOD = timedelta(days=31)
LOG_COLUMNS = (
    "created_at,request_id,method,endpoint,status_code,latency_ms,error_type,"
    "user_id,trip_id,model"
)


def require_dashboard_admin(
    request: Request,
    x_admin_token: str | None = Header(default=None, alias="X-Admin-Token"),
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
) -> None:
    """관리자 토큰을 확인한다. 실제 토큰은 백엔드 환경변수에만 둔다."""
    if os.getenv("DASHBOARD_AUTH_DISABLED", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }:
        return

    if credentials is not None:
        try:
            current_user = get_current_user(request, credentials)
        except HTTPException:
            current_user = None
        if current_user and is_dashboard_admin(current_user.email):
            return

    configured = os.getenv("DASHBOARD_ADMIN_TOKEN", "").strip()
    if not configured:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="대시보드 관리자 토큰이 백엔드에 설정되지 않았습니다.",
        )
    if not x_admin_token or not secrets.compare_digest(x_admin_token, configured):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="대시보드 관리자 인증이 필요합니다.",
        )


def _normalise_datetime(value: datetime | None, *, default: datetime) -> datetime:
    if value is None:
        return default
    if value.tzinfo is None:
        return value.replace(tzinfo=KST)
    return value.astimezone(KST)


def _period(start_at: datetime | None, end_at: datetime | None) -> DashboardPeriod:
    now = datetime.now(KST)
    end = _normalise_datetime(end_at, default=now)
    start = _normalise_datetime(
        start_at,
        default=end.replace(hour=0, minute=0, second=0, microsecond=0),
    )
    if start >= end:
        raise HTTPException(status_code=400, detail="start_at은 end_at보다 이전이어야 합니다.")
    if end - start > MAX_PERIOD:
        raise HTTPException(status_code=400, detail="조회 기간은 최대 31일까지 가능합니다.")
    return DashboardPeriod(start_at=start, end_at=end)


def _fetch_rows(client: Any, table_name: str, period: DashboardPeriod) -> list[dict[str, Any]]:
    query = (
        client.table(table_name)
        .select(LOG_COLUMNS if table_name == "api_request_logs" else "created_at")
        .gte("created_at", period.start_at.isoformat())
        .lt("created_at", period.end_at.isoformat())
        .range(0, 9999)
    )
    result = query.execute()
    return list(result.data or [])


def _status_code(row: dict[str, Any]) -> int | None:
    value = row.get("status_code")
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _is_success(row: dict[str, Any]) -> bool:
    code = _status_code(row)
    return code is not None and 200 <= code <= 399


def _latency(row: dict[str, Any]) -> int | None:
    value = row.get("latency_ms")
    if value is None:
        return None
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return None


def _created_at(row: dict[str, Any]) -> datetime:
    value = str(row.get("created_at") or "")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=KST)
    return parsed.astimezone(KST)


def _error_rate(failure_count: int, total_count: int) -> float:
    if total_count == 0:
        return 0.0
    return round(failure_count / total_count * 100, 2)


def _average_latency(rows: list[dict[str, Any]]) -> int:
    latencies = [value for row in rows if (value := _latency(row)) is not None]
    return round(sum(latencies) / len(latencies)) if latencies else 0


def _request_stats(rows: list[dict[str, Any]]) -> tuple[int, int, int]:
    success = sum(1 for row in rows if _is_success(row))
    return len(rows), success, len(rows) - success


def _hourly_stats(rows: list[dict[str, Any]]) -> list[HourlyRequestStat]:
    grouped: dict[datetime, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        occurred = _created_at(row).replace(minute=0, second=0, microsecond=0)
        grouped[occurred].append(row)
    result = []
    for hour in sorted(grouped):
        total, success, failure = _request_stats(grouped[hour])
        result.append(
            HourlyRequestStat(
                hour=hour,
                request_count=total,
                success_count=success,
                failure_count=failure,
            )
        )
    return result


def _endpoint_stats(rows: list[dict[str, Any]]) -> list[EndpointUsageStat]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[str(row.get("endpoint") or "unknown")].append(row)
    result = []
    for endpoint, endpoint_rows in grouped.items():
        total, success, failure = _request_stats(endpoint_rows)
        users = {str(row["user_id"]) for row in endpoint_rows if row.get("user_id")}
        result.append(
            EndpointUsageStat(
                endpoint=endpoint,
                request_count=total,
                unique_user_count=len(users),
                success_count=success,
                failure_count=failure,
                average_latency_ms=_average_latency(endpoint_rows),
                error_rate_percent=_error_rate(failure, total),
            )
        )
    return sorted(result, key=lambda item: (-item.request_count, item.endpoint))


def _llm_stats(rows: list[dict[str, Any]]) -> list[LlmSummaryStat]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        model = str(row.get("model") or "").strip()
        if model:
            grouped[model].append(row)
    result = []
    for model, model_rows in grouped.items():
        total, success, failure = _request_stats(model_rows)
        result.append(
            LlmSummaryStat(
                model=model,
                request_count=total,
                success_count=success,
                failure_count=failure,
                error_rate_percent=_error_rate(failure, total),
                average_latency_ms=_average_latency(model_rows),
                timeout_count=sum(row.get("error_type") == "gemini_timeout" for row in model_rows),
                gemini_error_count=sum(row.get("error_type") == "gemini_error" for row in model_rows),
                empty_response_count=sum(
                    row.get("error_type") == "gemini_empty_response" for row in model_rows
                ),
            )
        )
    return sorted(result, key=lambda item: (-item.request_count, item.model))


def _error_items(rows: list[dict[str, Any]], limit: int) -> list[ErrorLogItem]:
    filtered = [
        row
        for row in rows
        if _status_code(row) is None or _status_code(row) >= 400 or row.get("error_type")
    ]
    filtered.sort(key=_created_at, reverse=True)
    return [
        ErrorLogItem(
            occurred_at=_created_at(row),
            request_id=str(row.get("request_id") or ""),
            method=str(row.get("method") or ""),
            endpoint=str(row.get("endpoint") or ""),
            status_code=_status_code(row),
            latency_ms=_latency(row),
            error_type=str(row["error_type"]) if row.get("error_type") else None,
            user_id=str(row["user_id"]) if row.get("user_id") else None,
            trip_id=str(row["trip_id"]) if row.get("trip_id") else None,
            model=str(row["model"]) if row.get("model") else None,
        )
        for row in filtered[:limit]
    ]


def _load_dashboard_data(period: DashboardPeriod) -> tuple[list[dict[str, Any]], int]:
    client = get_service_client()
    request_rows = _fetch_rows(client, "api_request_logs", period)
    signup_rows = _fetch_rows(client, "profiles", period)
    return request_rows, len(signup_rows)


@router.get("/summary", response_model=DashboardSummaryResponse, dependencies=[Depends(require_dashboard_admin)])
def dashboard_summary(
    start_at: datetime | None = Query(default=None),
    end_at: datetime | None = Query(default=None),
) -> DashboardSummaryResponse:
    period = _period(start_at, end_at)
    try:
        request_rows, signup_count = _load_dashboard_data(period)
    except HTTPException:
        raise
    except Exception as error:
        raise HTTPException(status_code=500, detail="대시보드 통계를 조회하지 못했습니다.") from error

    total, success, failure = _request_stats(request_rows)
    return DashboardSummaryResponse(
        period=period,
        kpis=DashboardKpis(
            user_signup_count=signup_count,
            total_requests=total,
            success_count=success,
            failure_count=failure,
            error_rate_percent=_error_rate(failure, total),
            average_latency_ms=_average_latency(request_rows),
        ),
        hourly_requests=_hourly_stats(request_rows),
        endpoint_usage=_endpoint_stats(request_rows),
        llm_summary=_llm_stats(request_rows),
    )


@router.get("/endpoints", response_model=list[EndpointUsageStat], dependencies=[Depends(require_dashboard_admin)])
def dashboard_endpoints(
    start_at: datetime | None = Query(default=None),
    end_at: datetime | None = Query(default=None),
) -> list[EndpointUsageStat]:
    period = _period(start_at, end_at)
    try:
        request_rows = _fetch_rows(get_service_client(), "api_request_logs", period)
    except Exception as error:
        raise HTTPException(status_code=500, detail="엔드포인트 통계를 조회하지 못했습니다.") from error
    return _endpoint_stats(request_rows)


@router.get("/errors", response_model=ErrorLogResponse, dependencies=[Depends(require_dashboard_admin)])
def dashboard_errors(
    start_at: datetime | None = Query(default=None),
    end_at: datetime | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=100),
) -> ErrorLogResponse:
    period = _period(start_at, end_at)
    try:
        request_rows = _fetch_rows(get_service_client(), "api_request_logs", period)
    except Exception as error:
        raise HTTPException(status_code=500, detail="오류 로그를 조회하지 못했습니다.") from error
    items = _error_items(request_rows, limit)
    return ErrorLogResponse(items=items, count=len(items))
