from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query

from app.console_schemas import (
    ConsoleFeedbackSummary,
    ConsoleSystemStatusResponse,
    ConsoleUserDetail,
    ConsoleUserItem,
    ConsoleUserListResponse,
    FeedbackBreakdown,
    SystemStatusItem,
)
from app.db import get_service_client
from app.routers.dashboard import require_dashboard_admin

router = APIRouter(
    prefix="/admin/console",
    tags=["admin-console"],
    dependencies=[Depends(require_dashboard_admin)],
)


def _value(item: Any, key: str, default: Any = None) -> Any:
    if isinstance(item, dict):
        return item.get(key, default)
    return getattr(item, key, default)


def _rows(client: Any, table: str, columns: str) -> list[dict[str, Any]]:
    try:
        result = client.table(table).select(columns).execute()
        return list(result.data or [])
    except Exception:
        # activity_logs는 아직 배포되지 않은 환경에서도 콘솔을 열 수 있게 한다.
        return []


def _period_rows(client: Any, start: datetime, end: datetime) -> list[dict[str, Any]]:
    try:
        result = (
            client.table("api_request_logs")
            .select("created_at,endpoint,method,status_code,latency_ms,error_type,model")
            .gte("created_at", start.isoformat())
            .lt("created_at", end.isoformat())
            .range(0, 9999)
            .execute()
        )
        rows = list(result.data or [])
        return [
            row
            for row in rows
            if not str(row.get("endpoint") or "").startswith("/admin/")
        ]
    except Exception:
        return []


def _is_failed(row: dict[str, Any]) -> bool:
    try:
        status_code = int(row.get("status_code"))
    except (TypeError, ValueError):
        return True
    return status_code >= 400


def _p95_latency(rows: list[dict[str, Any]]) -> int:
    values = sorted(
        int(row["latency_ms"])
        for row in rows
        if row.get("latency_ms") is not None
        and str(row.get("latency_ms")).isdigit()
    )
    if not values:
        return 0
    index = max(0, (len(values) * 95 + 99) // 100 - 1)
    return values[index]


def _feedback_summary(
    rows: list[dict[str, Any]],
    trips: list[dict[str, Any]] | None = None,
) -> ConsoleFeedbackSummary:
    feedback_rows: list[dict[str, Any]] = []
    pace_rows: list[dict[str, Any]] = []
    feedback_breakdown: dict[str, int] = {}
    pace_breakdown: dict[str, int] = {}
    positive_count = 0
    negative_count = 0

    for row in rows:
        event_type = str(row.get("event_type") or "").casefold()
        metadata = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
        metadata_keys = {str(key).casefold() for key in metadata}
        is_feedback = "feedback" in event_type or "face" in event_type or bool(
            metadata_keys & {"rating", "sentiment", "feedback"}
        )
        is_pace = "pace" in event_type or bool(
            metadata_keys & {"pace", "travel_intensity", "intensity"}
        )
        if is_feedback:
            feedback_rows.append(row)
            label = str(row.get("event_type") or "기타 피드백")
            feedback_breakdown[label] = feedback_breakdown.get(label, 0) + 1
            sentiment = str(metadata.get("sentiment") or "").casefold()
            rating = metadata.get("rating")
            try:
                rating_value = float(rating)
            except (TypeError, ValueError):
                rating_value = None
            if sentiment in {"positive", "긍정", "good"} or (rating_value is not None and rating_value >= 4):
                positive_count += 1
            elif sentiment in {"negative", "부정", "bad"} or (rating_value is not None and rating_value <= 2):
                negative_count += 1
        if is_pace:
            pace_rows.append(row)
            pace = str(metadata.get("pace") or metadata.get("travel_intensity") or "기타")
            pace_breakdown[pace] = pace_breakdown.get(pace, 0) + 1

    # 현재 서비스의 페이스 선택값은 기존 trips.travel_intensity에 저장된다.
    # activity_logs에 별도 페이스 이벤트가 있는 경우에도 기존 로그를 존중하고,
    # 여행 조건에 저장된 선택값을 집계해 ADM-003에서 실제 사용량을 보여준다.
    for trip in trips or []:
        intensity = trip.get("travel_intensity")
        if intensity is None:
            continue
        pace = f"{intensity}/5"
        pace_rows.append(trip)
        pace_breakdown[pace] = pace_breakdown.get(pace, 0) + 1

    return ConsoleFeedbackSummary(
        feedback_count=len(feedback_rows),
        positive_count=positive_count,
        negative_count=negative_count,
        pace_count=len(pace_rows),
        feedback_breakdown=[
            FeedbackBreakdown(label=label, count=count)
            for label, count in sorted(feedback_breakdown.items(), key=lambda item: (-item[1], item[0]))
        ],
        pace_breakdown=[
            FeedbackBreakdown(label=label, count=count)
            for label, count in sorted(pace_breakdown.items(), key=lambda item: (-item[1], item[0]))
        ],
    )


def _service_name(endpoint: str, model: str | None) -> str:
    value = endpoint.casefold()
    if model or "/chat" in value:
        return "LLM 응답"
    if "place" in value or "accommodation" in value or "maps/search" in value:
        return "장소·영업시간 API"
    if "/map" in value or "route" in value:
        return "지도 SDK"
    return "일정표 렌더 서버"


def _system_status(rows: list[dict[str, Any]], start: datetime, end: datetime) -> ConsoleSystemStatusResponse:
    grouped: dict[str, list[dict[str, Any]]] = {
        "LLM 응답": [],
        "장소·영업시간 API": [],
        "지도 SDK": [],
        "일정표 렌더 서버": [],
    }
    for row in rows:
        grouped[_service_name(str(row.get("endpoint") or ""), row.get("model"))].append(row)

    services: list[SystemStatusItem] = []
    for service, service_rows in grouped.items():
        failed = sum(1 for row in service_rows if _is_failed(row))
        total = len(service_rows)
        failure_rate = round(failed / total * 100, 2) if total else 0.0
        p95 = _p95_latency(service_rows)
        service_status = "데이터 없음" if not total else ("지연" if failure_rate >= 5 or p95 >= 5000 else "정상")
        services.append(
            SystemStatusItem(
                service=service,
                status=service_status,
                request_count=total,
                failure_rate_percent=failure_rate,
                p95_latency_ms=p95,
            )
        )

    failed_total = sum(1 for row in rows if _is_failed(row))
    return ConsoleSystemStatusResponse(
        window_start=start,
        window_end=end,
        total_requests=len(rows),
        failure_count=failed_total,
        error_rate_percent=round(failed_total / len(rows) * 100, 2) if rows else 0.0,
        services=services,
    )


def _load_users(client: Any) -> list[Any]:
    result = client.auth.admin.list_users(page=1, per_page=1000)
    users = getattr(result, "users", result)
    return list(users or [])


def _parse_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed
    except ValueError:
        return None


def _latest_datetime(*values: Any) -> datetime | None:
    parsed = [value for value in (_parse_datetime(item) for item in values) if value]
    return max(parsed) if parsed else None


def _profile_map(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {str(row.get("id")): row for row in rows if row.get("id")}


def _user_item(
    user: Any,
    profile: dict[str, Any],
    trips: list[dict[str, Any]],
    requests: list[dict[str, Any]],
    activities: list[dict[str, Any]],
) -> ConsoleUserItem:
    user_id = str(_value(user, "id", ""))
    user_trips = [row for row in trips if str(row.get("user_id")) == user_id]
    user_requests = [row for row in requests if str(row.get("user_id")) == user_id]
    user_activities = [row for row in activities if str(row.get("user_id")) == user_id]
    last_active_at = _latest_datetime(
        _value(user, "last_sign_in_at"),
        *(row.get("created_at") for row in user_requests),
        *(row.get("created_at") for row in user_activities),
    )
    return ConsoleUserItem(
        id=user_id,
        email=str(_value(user, "email", "") or ""),
        username=str(profile.get("username") or ""),
        created_at=_parse_datetime(profile.get("created_at") or _value(user, "created_at")),
        last_active_at=last_active_at,
        trip_count=len(user_trips),
        request_count=len(user_requests),
        activity_count=len(user_activities),
    )


def _load_console_data(client: Any) -> tuple[list[Any], list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    users = _load_users(client)
    profiles = _rows(client, "profiles", "id,username,created_at")
    trips = _rows(
        client,
        "trips",
        "id,user_id,title,destination,start_date,end_date,status,updated_at",
    )
    requests = _rows(client, "api_request_logs", "user_id,created_at,endpoint,status_code,latency_ms")
    activities = _rows(client, "activity_logs", "id,user_id,trip_id,event_type,entity_type,entity_id,metadata,created_at")
    return users, profiles, trips, requests, activities


@router.get("/feedback", response_model=ConsoleFeedbackSummary, summary="사용자 피드백 요약 조회")
def console_feedback() -> ConsoleFeedbackSummary:
    """피드백·페이스 원문이 아닌 집계 결과만 반환한다."""

    client = get_service_client()
    return _feedback_summary(
        _rows(
            client,
            "activity_logs",
            "event_type,metadata,created_at",
        ),
        _rows(client, "trips", "travel_intensity"),
    )


@router.get("/system-status", response_model=ConsoleSystemStatusResponse, summary="시스템 상태 조회")
def console_system_status() -> ConsoleSystemStatusResponse:
    """최근 1시간의 API 요청 로그로 서비스 상태를 계산한다."""

    end = datetime.now(timezone.utc)
    start = end - timedelta(hours=1)
    return _system_status(_period_rows(get_service_client(), start, end), start, end)


@router.get("/users", response_model=ConsoleUserListResponse, summary="관리자용 사용자 목록 조회")
def console_users(
    search: str | None = Query(default=None, max_length=100),
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> ConsoleUserListResponse:
    try:
        users, profiles, trips, requests, activities = _load_console_data(get_service_client())
        profile_map = _profile_map(profiles)
        items = [
            _user_item(user, profile_map.get(str(_value(user, "id", "")), {}), trips, requests, activities)
            for user in users
        ]
    except Exception as error:
        raise HTTPException(status_code=500, detail="운영콘솔 사용자 정보를 조회하지 못했습니다.") from error

    if search:
        needle = search.strip().casefold()
        items = [item for item in items if needle in item.email.casefold() or needle in item.username.casefold()]
    items.sort(
        key=lambda item: item.created_at or datetime.min.replace(tzinfo=timezone.utc),
        reverse=True,
    )
    total = len(items)
    return ConsoleUserListResponse(items=items[offset : offset + limit], count=min(limit, max(total - offset, 0)), total=total)


@router.get("/users/{user_id}", response_model=ConsoleUserDetail, summary="관리자용 사용자 상세 조회")
def console_user_detail(user_id: str) -> ConsoleUserDetail:
    try:
        users, profiles, trips, requests, activities = _load_console_data(get_service_client())
    except Exception as error:
        raise HTTPException(status_code=500, detail="운영콘솔 사용자 정보를 조회하지 못했습니다.") from error

    user = next((item for item in users if str(_value(item, "id", "")) == user_id), None)
    if user is None:
        raise HTTPException(status_code=404, detail="사용자를 찾을 수 없습니다.")

    profile = _profile_map(profiles).get(user_id, {})
    item = _user_item(user, profile, trips, requests, activities)
    return ConsoleUserDetail(
        **item.model_dump(),
        trips=[row for row in trips if str(row.get("user_id")) == user_id],
        recent_requests=[row for row in requests if str(row.get("user_id")) == user_id][:50],
        recent_activities=[row for row in activities if str(row.get("user_id")) == user_id][:50],
    )
