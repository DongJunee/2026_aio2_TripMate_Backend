import logging
from collections import Counter
from datetime import date, datetime, time, timedelta, timezone
from typing import Literal
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi import APIRouter, Depends, HTTPException, Response, status
from fastapi import APIRouter, Depends, HTTPException, status

from app.activity_logging import record_activity
from app.db import get_user_client
from app.deps import CurrentUser, get_current_user
from app.google_maps_client import (
    GoogleMapsClient,
    GoogleMapsError,
    GoogleMapsUnavailableError,
)
from app.services.itinerary_generation import generate_daily_itinerary_drafts
from app.services.itinerary_routing import group_nearby_itinerary_places
from app.services.itinerary_export import generate_itinerary_images, itinerary_as_text
from app.services.destination_scope import resolve_destination_scope
from app.schemas import (
    ItineraryItemCreate,
    ItineraryItemTimeUpdate,
    ItineraryItemUpdate,
    ItineraryPlaceSwap,
    TripCreate,
    TripDateRangeUpdate,
    TripDayCreate,
    TripPinUpdate,
    TripUpdate,
)
from fastapi import APIRouter, Depends, HTTPException, status

router = APIRouter(tags=["trips"])
LOGGER = logging.getLogger(__name__)


# 일정 한 줄은 시간 칸이고, 위·아래 버튼은 두 시간 칸 안의 장소 정보만
# 교환한다. 이렇게 해야 시간 변경 기록을 나중에 장소 순서와 독립적으로 되돌릴 수 있다.
_ITEM_SNAPSHOT_FIELDS = (
    "id",
    "trip_day_id",
    "place_id",
    "title",
    "item_type",
    "source",
    "start_at",
    "end_at",
    "estimated_stay_minutes",
    "is_fixed",
    "travel_mode",
    "notes",
    "sort_order",
)
_PLACE_SLOT_FIELDS = (
    "place_id",
    "title",
    "item_type",
    "source",
    "travel_mode",
    "notes",
)


def _item_snapshot(item: dict) -> dict:
    """Undo에 필요한 일정 행의 값만 JSON으로 안전하게 복사한다."""

    return {field: item.get(field) for field in _ITEM_SNAPSHOT_FIELDS}


def _change_data(items: list[dict], summary: dict | None = None) -> dict:
    """변경 로그의 JSONB 본문을 일정 목록과 화면용 요약으로 만든다."""

    value: dict[str, object] = {"items": [_item_snapshot(item) for item in items]}
    if summary:
        value["summary"] = summary
    return value


def _record_itinerary_change(
    client,
    trip_id: UUID | str,
    *,
    action_type: str,
    actor_type: str,
    before_data: dict | None,
    after_data: dict,
    undo_of_log_id: UUID | str | None = None,
) -> dict:
    """한 번의 일정 변경과 Undo에 필요한 전후 상태를 감사 로그에 저장한다."""

    values: dict[str, object] = {
        "trip_id": str(trip_id),
        "action_type": action_type,
        "actor_type": actor_type,
        "before_data": before_data,
        "after_data": after_data,
    }
    if undo_of_log_id is not None:
        values["undo_of_log_id"] = str(undo_of_log_id)
    try:
        result = client.table("itinerary_change_logs").insert(values).execute()
    except Exception as error:
        LOGGER.warning("일정 변경 로그 저장 실패 (%s).", type(error).__name__)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="일정 변경 기록을 저장하지 못했습니다. Supabase 로그 테이블 설정을 확인하세요.",
        ) from error
    if not result.data:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="일정 변경 기록을 저장하지 못했습니다.",
        )
    return result.data[0]


def _clock_text(value: object, timezone_name: object) -> str:
    """저장된 UTC 시각을 여행지 현지 시각의 HH:MM 문자열로 바꾼다."""

    try:
        parsed = value if isinstance(value, datetime) else datetime.fromisoformat(
            str(value).replace("Z", "+00:00")
        )
        zone = ZoneInfo(str(timezone_name or "Asia/Seoul"))
        # 과거 데이터처럼 시간대가 빠진 값은 서버 시간대가 아닌 여행지 현지
        # 벽시계 시각으로 해석한다.
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=zone)
        else:
            parsed = parsed.astimezone(zone)
        return parsed.strftime("%H:%M")
    except (TypeError, ValueError, ZoneInfoNotFoundError):
        return "시간 미정"


def _change_status(log: dict, timezone_name: object) -> dict:
    """프론트의 일정 변경 상태 카드에 필요한 안전한 로그 요약을 반환한다."""

    action_type = str(log.get("action_type") or "")
    after_data = log.get("after_data") if isinstance(log.get("after_data"), dict) else {}
    before_data = log.get("before_data") if isinstance(log.get("before_data"), dict) else {}
    summary = after_data.get("summary") if isinstance(after_data.get("summary"), dict) else {}
    message = str(summary.get("message") or "일정이 변경되었습니다.")
    detail = str(summary.get("detail") or "")
    # 이미 저장된 로그도 전후 스냅샷은 UTC 시각을 갖고 있다. 화면을 다시 불러올
    # 때 여행 시간대로 문구를 조립하면, 예전 카드까지 UTC가 아닌 현지 시각으로
    # 바로잡을 수 있다.
    before_items = before_data.get("items") if isinstance(before_data.get("items"), list) else []
    after_items = after_data.get("items") if isinstance(after_data.get("items"), list) else []
    if action_type == "time_changed" and before_items and after_items:
        before_item, after_item = before_items[0], after_items[0]
        if isinstance(before_item, dict) and isinstance(after_item, dict):
            detail = (
                f"{_clock_text(before_item.get('start_at'), timezone_name)}"
                f"–{_clock_text(before_item.get('end_at'), timezone_name)}"
                f" → {_clock_text(after_item.get('start_at'), timezone_name)}"
                f"–{_clock_text(after_item.get('end_at'), timezone_name)}"
            )
    is_undo = action_type == "undo"
    can_undo = (
        not is_undo
        and not bool(log.get("is_reverted"))
    )
    return {
        "id": log.get("id"),
        "action_type": action_type,
        "message": message,
        "detail": detail,
        "created_at": log.get("created_at"),
        "can_undo": can_undo,
    }


def _local_datetime_for_day(day: dict, clock_time: time, timezone_name: object) -> datetime:
    """DAY 날짜와 사용자가 고른 시각으로 여행지 시간대 datetime을 만든다."""

    try:
        travel_date = date.fromisoformat(str(day["travel_date"]))
        zone = ZoneInfo(str(timezone_name or "Asia/Seoul"))
    except (KeyError, TypeError, ValueError, ZoneInfoNotFoundError) as error:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="여행 DAY의 날짜 또는 시간대 정보를 확인할 수 없습니다.",
        ) from error
    return datetime.combine(travel_date, clock_time).replace(tzinfo=zone)


def _ordered_day_items(client, trip_id: UUID | str, day_id: UUID | str) -> list[dict]:
    """화면·지도와 같은 시간 우선 기준으로 특정 DAY의 일정 칸을 반환한다."""

    items = (
        client.table("itinerary_items")
        .select("*")
        .eq("trip_id", str(trip_id))
        .eq("trip_day_id", str(day_id))
        .execute()
        .data
    )
    return sorted(
        items,
        key=lambda item: (
            item.get("start_at") is None,
            str(item.get("start_at") or ""),
            int(item.get("sort_order") or 0),
        ),
    )


def _owned_trip(client, trip_id: UUID | str) -> dict:
    """현재 사용자가 접근할 수 있는 여행을 반환하고, 없으면 404 오류를 발생시킨다."""

    # `client`에는 현재 사용자 토큰이 들어 있으므로 Supabase RLS 정책도
    # 소유권 확인 역할을 한다.
    result = client.table("trips").select("*").eq("id", str(trip_id)).execute()
    if not result.data:
        raise HTTPException(status_code=404, detail="여행을 찾을 수 없습니다.")
    return result.data[0]


def _owned_day(client, trip_id: UUID | str, day_id: UUID | str) -> dict:
    """현재 사용자가 접근할 수 있는 지정 여행에 속한 DAY만 반환한다."""

    result = (
        client.table("trip_days")
        .select("*")
        .eq("id", str(day_id))
        .eq("trip_id", str(trip_id))
        .execute()
    )
    if not result.data:
        raise HTTPException(status_code=400, detail="이 여행에 속한 일차가 아닙니다.")
    return result.data[0]


def touch_trip(client, trip_id: UUID | str) -> None:
    """일정 또는 채팅 변경을 여행 자체의 수정으로 기록한다.

    사이드바의 일반 여행은 ``trips.updated_at`` 기준으로 정렬된다.
    따라서 여행 제목뿐 아니라 날짜, 일정, 대화가 바뀌었을 때도 이 시간을
    갱신하여 해당 여행이 목록 상단에 가깝게 표시되도록 한다.
    """

    client.table("trips").update(
        {"updated_at": datetime.now(timezone.utc).isoformat()}
    ).eq("id", str(trip_id)).execute()


def _pinned_trips_for_user(client, user_id: str) -> list[dict]:
    """한 사용자의 고정 여행을 저장된 사이드바 순서대로 반환한다.

    사용자 토큰이 포함된 Supabase 클라이언트가 RLS를 계속 적용한다.
    여기에 사용자 ID 조건도 함께 두어 고정 순서가 여행 소유자별 데이터임을
    명확히 하고, 이 함수를 다른 보호된 API에서 재사용해도 안전하게 만든다.
    """

    trips = (
        client.table("trips")
        .select("id,pinned_order")
        .eq("user_id", user_id)
        .execute()
        .data
    )
    pinned_trips = [trip for trip in trips if trip.get("pinned_order") is not None]
    # 수동 수정한 테이블에 중복된 pinned_order 값이 잠시 생겨도 복구 결과가
    # 일정하도록, 대체 정렬 기준으로 ID를 사용한다.
    return sorted(
        pinned_trips,
        key=lambda trip: (int(trip["pinned_order"]), str(trip["id"])),
    )


def _normalize_pinned_orders(client, user_id: str, pinned_trips: list[dict]) -> None:
    """사용자의 고정 순서를 0부터 끊김 없이 다시 저장한다.

    새 고정은 이 정리 이후 마지막 순서에 추가된다. 고정을 해제하면 먼저
    해당 값을 비우고 이 함수를 호출하여 ``0, 1, 3`` 같은 빈 순서 없이
    뒤의 여행들이 앞으로 한 칸씩 이동하도록 한다.
    """

    for expected_order, trip in enumerate(pinned_trips):
        if int(trip["pinned_order"]) == expected_order:
            continue
        # RLS가 이미 적용됐어도 소유자 조건을 유지한다. 인증된 요청 사용자의 여행이
        # 아닌 데이터를 수정하지 않도록 한 번 더 막는 장치이다.
        client.table("trips").update({"pinned_order": expected_order}).eq(
            "id", trip["id"]
        ).eq("user_id", user_id).execute()


def _initial_trip_days(payload: TripCreate) -> list[dict]:
    """DB에 쓰기 전 AI 초안 검증에 사용할 새 여행의 DAY 행을 만든다."""

    if payload.start_date is None or payload.end_date is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="AI 일정 초안을 만들려면 여행 시작일과 종료일이 필요합니다.",
        )

    day_count = (payload.end_date - payload.start_date).days + 1
    return [
        {
            # Gemini가 신뢰할 수 없는 DAY ID를 만들지 못하게, 서버가 미리 UUID를
            # 발급한다. 나중에 이 같은 ID를 trip_days에 저장한다.
            "id": str(uuid4()),
            "day_number": number,
            "travel_date": (payload.start_date + timedelta(days=number - 1)).isoformat(),
            "title": f"DAY {number}",
        }
        for number in range(1, day_count + 1)
    ]


def _cache_google_place(client, values: dict) -> dict:
    """검증된 Google 장소를 공용 캐시에 저장하고 내부 UUID 행을 반환한다."""

    place_values = dict(values)
    place_values["provider_place_id"] = place_values.get(
        "provider_place_id"
    ) or place_values.get("google_place_id")
    try:
        result = (
            client.table("places")
            .upsert(place_values, on_conflict="google_place_id")
            .execute()
        )
    except Exception as error:
        # places는 여러 사용자의 일정이 함께 참조할 수 있는 Google 캐시다. 하지만
        # 이 저장이 실패하면 이번 여행에는 실제 장소를 연결할 수 없으므로 생성도
        # 중단한다. Supabase 상세는 서버 로그에만 남긴다.
        LOGGER.warning("AI 일정 Google 장소 캐시 저장 실패 (%s).", type(error).__name__)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Google 장소 정보를 저장하지 못했습니다. Supabase places 설정을 확인하세요.",
        ) from error

    if result.data and result.data[0].get("id"):
        return result.data[0]

    # RETURNING 정책이 제한된 Supabase 프로젝트에서도 성공한 upsert의 UUID를
    # 얻을 수 있도록 한 번만 다시 읽는다.
    try:
        fetched = (
            client.table("places")
            .select("id")
            .eq("google_place_id", place_values["google_place_id"])
            .limit(1)
            .execute()
            .data
        )
    except Exception as error:
        LOGGER.warning("AI 일정 Google 장소 캐시 재조회 실패 (%s).", type(error).__name__)
        fetched = []
    if fetched:
        return fetched[0]
    raise HTTPException(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        detail="저장한 Google 장소 정보를 찾지 못했습니다. Supabase places 설정을 확인하세요.",
    )


# 검색어 하나당 Google 에 요청할 후보 수. 도시 검증에서 인접 도시 후보가 빠지는
# 만큼 여유를 둔다. Places 텍스트 검색의 상한은 20이다.
_ITINERARY_PLACE_CANDIDATES = 10


def _candidate_origin(scope, place) -> dict[str, str]:
    """검증에 떨어진 후보가 어느 도시 소속인지만 짧게 요약한다.

    Google 응답 전체를 남기지 않으려고 판정에 쓰인 주소 구성요소 이름과 사유만
    모은다. 좌표·평점·장소 ID 같은 나머지 필드는 기록하지 않는다.
    """

    def component(kind: str) -> str:
        return next(
            (
                part.long_text or part.short_text
                for part in place.address_components
                if kind in part.types
            ),
            "",
        )

    return {
        "name": place.display_name,
        "city": component(scope.city_type),
        "area": component("administrative_area_level_1"),
        "reason": scope.rejection_reason(place) or "",
    }


def _resolve_initial_itinerary_places(
    client,
    trip_values: dict,
    drafts: list[dict],
) -> list[dict]:
    """AI 검색어마다 실제 Google 장소를 연결한 저장용 일정 행을 만든다."""

    destination = str(trip_values.get("destination") or "").strip()
    if not destination:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="AI 일정 초안을 만들려면 여행지를 입력하세요.",
        )

    try:
        maps = GoogleMapsClient.from_environment()
    except GoogleMapsUnavailableError as error:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="AI 장소 일정을 만들려면 Google Maps API를 설정하세요.",
        ) from error

    try:
        destination_scope = resolve_destination_scope(maps, destination)
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    except GoogleMapsError as error:
        LOGGER.warning("여행 도시 Google 조회 실패: %s", error)
        raise HTTPException(
            status_code=502,
            detail="Google Places에서 여행 도시 범위를 확인하지 못했습니다. 잠시 후 다시 시도해 주세요.",
        ) from error

    resolved_rows: list[dict] = []
    place_coordinates: dict[str, tuple[float, float]] = {}
    # 도시 조회 1회 뒤, 같은 여행 생성 중 반복되는 장소 검색은 재사용한다.
    # Google 장소 검색은 서로 다른 검색어 수만큼이며 숙소·출국 안내는 검색하지 않는다.
    resolved_by_query: dict[str, dict] = {}
    city_aliases_checked = False
    for draft in drafts:
        row = dict(draft)
        activity_only = row.pop("_activity_only", False)
        if activity_only:
            # 이 표시는 모델 원문이 아니라 일정 생성 서비스가 만든 숙소·휴식 행이다.
            # 숙소가 아직 없으므로 가짜 장소 ID/좌표를 만들지 않고 시간 계획만 저장한다.
            if row.get("item_type") not in {"hotel", "note"} or row.get("_place_query"):
                raise HTTPException(status_code=502, detail="숙소·휴식 일정 형식이 올바르지 않습니다.")
            row.pop("_place_query", None)
            resolved_rows.append(row)
            continue

        place_query = str(draft.get("_place_query") or "").strip()
        if not place_query:
            # 이 값은 일정 생성 서비스의 내부 계약이므로, 없으면 모델 결과가 잘못된
            # 것이다. 아직 trips 행을 만들기 전이므로 불완전한 여행이 남지 않는다.
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="AI가 Google 장소 검색어를 올바르게 만들지 못했습니다. 다시 시도해 주세요.",
            )
        full_query = " ".join((place_query, destination))
        query_key = full_query.casefold()
        if query_key in resolved_by_query:
            row.pop("_place_query", None)
            row.update(resolved_by_query[query_key])
            resolved_rows.append(row)
            continue
        try:
            # [변경 사유] Google 의 도시 범위는 사각형이라 인접 도시 장소가 함께
            # 돌아온다. 아래 도시 검증에서 그런 후보를 걸러내므로, 후보가 적으면
            # 도시 안 장소가 목록에 들지 못해 여행 생성 전체가 막힌다. 후보 수를
            # 늘려도 Places 텍스트 검색은 요청 단위로 과금되어 호출 수는 그대로다.
            candidates = maps.search_places(
                full_query,
                max_results=_ITINERARY_PLACE_CANDIDATES,
                language_code="ko",
                location_restriction=destination_scope.viewport,
                include_region_metadata=True,
            )
        except GoogleMapsError as error:
            # LOGGER.warning("AI 일정 Google 장소 검색 실패 (%s).", type(error).__name__)
            LOGGER.warning("AI 일정 Google 장소 검색 실패: %s", error)
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="Google Places에서 AI 일정 장소를 찾지 못했습니다. 잠시 후 다시 시도해 주세요.",
            ) from error

        # Google에 검색 범위를 지정해도 응답을 다시 검사한다. 같은 도에 있어도
        # 다른 도시이거나, 도시 주소 정보가 없으면 후보를 저장하지 않는다.
        place = next((item for item in candidates if destination_scope.accepts(item)), None)
        if place is None and not city_aliases_checked and any(
            destination_scope.rejection_reason(item) in {"city_name_mismatch", "administrative_name_mismatch"}
            for item in candidates
        ):
            # 후쿠오카 도시 응답은 '후쿠오카시', 장소 주소는 'Fukuoka'처럼 언어가
            # 달라질 수 있다. 요청당 한 번 같은 도시 ID의 영문 주소만 보완한다.
            # 후보의 이름을 무조건 별칭으로 추가하거나 지역 검사를 해제하지 않는다.
            city_aliases_checked = True
            try:
                city_aliases = maps.get_city_details(destination_scope.google_place_id, language_code="en")
                destination_scope = destination_scope.with_city_aliases(city_aliases)
            except (GoogleMapsError, ValueError) as error:
                LOGGER.warning("여행 도시의 언어별 주소 확인 실패 (%s).", type(error).__name__)
                raise HTTPException(
                    status_code=502,
                    detail="Google에서 같은 도시의 언어별 주소를 확인하지 못했습니다. 잠시 후 다시 시도해 주세요.",
                ) from error
            place = next((item for item in candidates if destination_scope.accepts(item)), None)
        if place is None:
            # 인증값·사용자 질문·전체 응답은 기록하지 않고 실패 원인별 개수만 남긴다.
            rejected = Counter(destination_scope.rejection_reason(item) for item in candidates)
            LOGGER.warning("AI 일정 장소 지역 검증 실패: 후보 %d개, 사유 %s", len(candidates), dict(rejected))
            # [변경 사유] 개수만으로는 '모델이 다른 도시를 추천했다' 와 '주소 표기가
            # 어긋났다' 를 구분할 수 없어 원인 확인이 불가능했다. 검색어와 후보의
            # 도시·상위 지역 이름만 DEBUG 로 남긴다. 인증값과 Google 전체 응답은
            # 그대로 기록하지 않는다.
            if LOGGER.isEnabledFor(logging.DEBUG):
                LOGGER.debug(
                    "AI 일정 장소 지역 검증 실패 상세: 검색어=%r 도시=%r 후보=%s",
                    full_query,
                    destination_scope.display_name,
                    [_candidate_origin(destination_scope, item) for item in candidates],
                )
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=(
                    "AI가 제안한 장소 중 여행 도시 안에 있다고 확인된 후보가 없습니다. "
                    "다른 도시의 장소로 대체하지 않았습니다. 다시 시도해 주세요."
                ) if candidates else "Google 장소 검색 결과가 없습니다. 도시와 국가를 함께 입력하거나 다시 시도해 주세요.",
            )

        cached_place = _cache_google_place(client, place.as_place_row())
        if place.coordinates is not None:
            place_coordinates[str(cached_place["id"])] = (
                place.coordinates.latitude, place.coordinates.longitude,
            )
        row.pop("_place_query", None)
        row["place_id"] = cached_place["id"]
        row["title"] = place.display_name[:150]
        row["source"] = "ai_recommendation"
        resolved_by_query[query_key] = {
            key: row[key] for key in ("place_id", "title", "source")
        }
        resolved_rows.append(row)

    # 실제 Google 좌표가 모두 모인 뒤, DB 저장 전에 날짜 간 군집과 방문 순서를
    # 개선한다. 좌표는 메모리에서만 사용하며 일정 테이블에 임시 컬럼을 보내지 않는다.
    return group_nearby_itinerary_places(resolved_rows, place_coordinates)


def _remove_incomplete_trip(client, trip_id: UUID | str, user_id: str) -> None:
    """새 여행 저장 중 DB 오류가 나면 해당 요청이 만든 행만 정리한다."""

    # PostgREST 호출은 여러 SQL 문을 하나의 트랜잭션으로 묶지 않는다. 예외적으로
    # trips 삽입 뒤 DAY 또는 일정 저장이 실패하면, 방금 만든 여행만 역순으로 지워
    # 사용자의 목록에 빈 여행이 남지 않게 한다.
    try:
        client.table("itinerary_items").delete().eq("trip_id", str(trip_id)).execute()
        client.table("trip_days").delete().eq("trip_id", str(trip_id)).execute()
        client.table("trips").delete().eq("id", str(trip_id)).eq("user_id", user_id).execute()
    except Exception as error:
        LOGGER.error("불완전한 새 여행 정리 실패 (%s).", type(error).__name__)


def _attach_cached_places(client, items: list[dict]) -> None:
    """일정에 연결된 Google 장소 정보를 대시보드 응답에 한 번만 붙인다."""

    place_ids = sorted(
        {str(item["place_id"]) for item in items if item.get("place_id")}
    )
    if not place_ids:
        return

    try:
        place_rows = (
            client.table("places")
            .select(
                "id,display_name,formatted_address,google_rating,"
                "google_rating_count,google_maps_uri,latitude,longitude,google_place_id"
            )
            .in_("id", place_ids)
            .execute()
            .data
        )
    except Exception as error:
        # 오래된 장소 마이그레이션을 아직 적용하지 않은 기존 여행도 대시보드 자체는
        # 열 수 있어야 한다. 새 AI 여행은 캐시 저장 단계에서 이미 이 문제를 알려 준다.
        LOGGER.warning("대시보드 Google 장소 조회 실패 (%s).", type(error).__name__)
        return

    places_by_id = {str(place["id"]): place for place in place_rows if place.get("id")}
    for item in items:
        place = places_by_id.get(str(item.get("place_id") or ""))
        if place:
            item["place"] = place


def _attach_accommodation_place(client, trip: dict) -> None:
    """여행에 지정된 숙소의 Google 장소 정보를 대시보드 응답에 붙인다.

    숙소를 아직 정하지 않은 여행과 숙소 SQL 마이그레이션 전의 기존 여행은 아무
    변경 없이 통과한다. 장소 캐시를 읽지 못해도 일정 대시보드 자체는 열려야 한다.
    """

    accommodation_place_id = trip.get("accommodation_place_id")
    if not accommodation_place_id:
        return
    try:
        result = (
            client.table("places")
            .select(
                "id,display_name,formatted_address,google_rating,"
                "google_rating_count,google_maps_uri,latitude,longitude,google_place_id"
            )
            .eq("id", str(accommodation_place_id))
            .limit(1)
            .execute()
        )
    except Exception as error:
        LOGGER.warning("여행 숙소 Google 장소 조회 실패 (%s).", type(error).__name__)
        return
    if result.data:
        trip["accommodation_place"] = result.data[0]


def trip_dashboard(client, trip_id: UUID | str) -> dict:
    """여행, DAY 목록, DAY별로 묶인 일정 항목을 대시보드 응답으로 만든다."""

    # 사용자가 다른 사용자의 대시보드를 요청하지 못하도록 먼저 부모 여행을 조회한다.
    trip = _owned_trip(client, trip_id)
    days = (
        client.table("trip_days")
        .select("*")
        .eq("trip_id", str(trip_id))
        .order("day_number")
        .execute()
        .data
    )
    items = (
        client.table("itinerary_items")
        .select("*")
        .eq("trip_id", str(trip_id))
        .order("sort_order")
        .execute()
        .data
    )
    _attach_cached_places(client, items)
    _attach_accommodation_place(client, trip)
    items_by_day: dict[str, list[dict]] = {}
    for item in items:
        day_id = item.get("trip_day_id")
        if day_id:
            items_by_day.setdefault(day_id, []).append(item)

    for day in days:
        # 데이터베이스에 중복 데이터를 저장하지 않으면서, 프론트엔드가 쓰기 쉬운
        # 중첩 응답을 만들기 위해 DAY별 항목을 붙인다.
        day["items"] = items_by_day.get(day["id"], [])

    return {"trip": trip, "days": days}


def _shift_datetime(value: str, day_delta: timedelta, timezone_name: str) -> str:
    """여행지 벽시계 시각을 유지하며 날짜를 옮기고 서머타임도 다시 적용한다."""

    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    trip_timezone = ZoneInfo(timezone_name)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=trip_timezone)
    local_time = parsed.astimezone(trip_timezone)
    return (local_time + day_delta).isoformat()


def _sync_trip_days_for_dates(
    client,
    trip_id: UUID | str,
    start_date: date,
    end_date: date,
    timezone_name: str,
) -> None:
    """등록된 일정을 보존하면서 trip_days를 변경된 여행 기간에 맞춘다.

    기존 DAY ID는 유지한다. 여행 시작일이 이동하면 연결된 일정 시간도 같은
    일수만큼 옮기고, 종료일을 늘리면 빈 DAY 행을 추가한다. 일정이 이미 있는
    DAY를 포함하도록 기간을 줄이는 일은 허용하지 않는다.
    """

    # 날짜 행을 수정하기 전에 시간대가 유효한지 먼저 확인한다.
    try:
        ZoneInfo(timezone_name)
    except (ZoneInfoNotFoundError, ValueError) as error:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="여행지 시간대 정보를 확인할 수 없어 날짜를 변경하지 못했습니다.",
        ) from error

    # 행을 바꾸기 전에 현재 일정 전체를 읽어, 기존 일정 항목이 사라지게 되는
    # 기간 축소 요청을 거부할 수 있게 한다.
    desired_day_count = (end_date - start_date).days + 1
    existing_days = (
        client.table("trip_days")
        .select("id,day_number,travel_date")
        .eq("trip_id", str(trip_id))
        .order("day_number")
        .execute()
        .data
    )
    itinerary_items = (
        client.table("itinerary_items")
        .select("id,trip_day_id,start_at,end_at")
        .eq("trip_id", str(trip_id))
        .execute()
        .data
    )
    items_by_day: dict[str, list[dict]] = {}
    for item in itinerary_items:
        if item.get("trip_day_id"):
            items_by_day.setdefault(item["trip_day_id"], []).append(item)

    removed_days = [
        day for day in existing_days if int(day["day_number"]) > desired_day_count
    ]
    removed_day_ids = {day["id"] for day in removed_days}
    if any(item.get("trip_day_id") in removed_day_ids for item in itinerary_items):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "줄어드는 여행 기간의 DAY에 등록된 일정이 있습니다. "
                "해당 일정을 옮기거나 삭제한 뒤 다시 시도하세요."
            ),
        )

    kept_day_numbers: set[int] = set()
    for day in existing_days:
        day_number = int(day["day_number"])
        if day_number > desired_day_count:
            continue
        kept_day_numbers.add(day_number)
        old_date = date.fromisoformat(str(day["travel_date"]))
        new_date = start_date + timedelta(days=day_number - 1)
        if old_date == new_date:
            continue

        client.table("trip_days").update(
            {"travel_date": new_date.isoformat()}
        ).eq("id", day["id"]).execute()

        # DAY를 옮기면 연결된 일정 시각도 옮기되, 시간대의 시각은 유지한다.
        # 예를 들어 오전 9시 비행기는 계속 오전 9시로 남는다.
        day_delta = new_date - old_date
        for item in items_by_day.get(day["id"], []):
            changed_times = {
                field: _shift_datetime(item[field], day_delta, timezone_name)
                for field in ("start_at", "end_at")
                if item.get(field)
            }
            if changed_times:
                client.table("itinerary_items").update(changed_times).eq(
                    "id", item["id"]
                ).execute()

    if removed_days:
        # 위의 일정 항목 안전 확인이 성공한 뒤에만 이 코드를 실행한다.
        client.table("trip_days").delete().eq("trip_id", str(trip_id)).gt(
            "day_number", desired_day_count
        ).execute()

    new_days = [
        {
            "trip_id": str(trip_id),
            "day_number": day_number,
            "travel_date": (start_date + timedelta(days=day_number - 1)).isoformat(),
            "title": f"DAY {day_number}",
        }
        for day_number in range(1, desired_day_count + 1)
        if day_number not in kept_day_numbers
    ]
    if new_days:
        # 여행 기간을 늘리면 새로 추가한 날짜에 빈 DAY 행을 만든다.
        client.table("trip_days").insert(new_days).execute()


@router.get("/me/trips", summary="내 여행 목록 조회")
def list_my_trips(current_user: CurrentUser = Depends(get_current_user)):
    """로그인한 사용자의 여행을 최근 활동 순으로 반환한다."""

    # RLS가 결과를 Bearer 토큰 사용자 소유 여행으로 제한한다.
    client = get_user_client(current_user.token)
    return (
        client.table("trips")
        .select("*")
        .order("updated_at", desc=True)
        .execute()
        .data
    )


@router.post(
    "/me/trips", status_code=status.HTTP_201_CREATED, summary="AI 여행 일정 생성",
    response_description="생성된 여행과 일차별 일정 정보",
    responses={401: {"description": "로그인이 필요함"}, 500: {"description": "여행 또는 일정 저장 실패"}, 502: {"description": "AI 일정 초안 또는 Google Places 검증 실패"}},
)
def create_my_trip(
    payload: TripCreate,
    current_user: CurrentUser = Depends(get_current_user),
):
    """Google Places로 검증된 AI 일정까지 준비된 경우에만 여행을 생성한다."""

    client = get_user_client(current_user.token)
    trip_values = payload.model_dump(mode="json")
        # must_visit 은 trips 테이블에 없는 칼럼이다. trip_values 는
    # 아래(trips.py:776)에서 그대로 insert 되므로, 여기서 빼지 않으면 삽입이
    # 통째로 실패한다. 초안 생성에만 쓰는 값이라 사본으로만 넘긴다.
    must_visit = trip_values.pop("must_visit", [])
    # [변경 사유] 이름만 프롬프트에 넣는다. google_place_id 는 모델이 쓸 값이
    # 아니고, 정확한 지점 반영은 아래 6·7단계(선택)에서 다룬다.
    must_visit_names = [
        text for place in must_visit
        if (text := str(place.get("name") or "").strip())
    ]

    days = _initial_trip_days(payload)

    # Gemini와 Google Places 조회는 trips 행을 만들기 전에 끝낸다. 따라서 둘 중
    # 하나라도 실패하면 사용자의 여행 목록에는 새 여행이 전혀 생기지 않는다.
    try:
        # trip_values 자체를 오염시키지 않으려고 사본을 만든다.
        # 이 dict 는 프롬프트 입력으로만 흐르고 DB 로는 가지 않는다.
        generated = generate_daily_itinerary_drafts(
            {**trip_values, "must_visit": must_visit_names}, days
        )
    except ValueError as error:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="AI 일정 초안을 만들지 못했습니다. 다시 시도해 주세요.",
        ) from error
    drafts = generated.items
    if not drafts:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="AI 일정 초안이 비어 있어 여행을 만들지 못했습니다. 다시 시도해 주세요.",
        )
    # 일정 시각에 적용한 것과 같은 시간대를 저장해 UTC 조회 결과를 화면에서
    # 여행지 현지 시각으로 되돌릴 수 있게 한다. 서버의 로컬 시간은 사용하지 않는다.
    trip_values["timezone"] = generated.timezone
    resolved_drafts = _resolve_initial_itinerary_places(client, trip_values, drafts)

    # Google 결과까지 모두 준비된 뒤에만 사용자 소유 여행을 만들고, 같은 서버가
    # 미리 발급한 DAY UUID와 실제 place_id를 한 번에 연결한다.
    trip_values["user_id"] = current_user.id
    trip: dict | None = None
    try:
        trip_result = client.table("trips").insert(trip_values).execute()
        if not trip_result.data:
            raise RuntimeError("새 여행 행을 반환받지 못했습니다.")
        trip = trip_result.data[0]

        persisted_days = [
            {**day, "trip_id": trip["id"]}
            for day in days
        ]
        day_result = client.table("trip_days").insert(persisted_days).execute()
        if len(day_result.data or []) != len(persisted_days):
            raise RuntimeError("새 여행 DAY 행을 모두 저장하지 못했습니다.")

        itinerary_rows = []
        for sort_order, draft in enumerate(resolved_drafts):
            row = dict(draft)
            row["trip_id"] = trip["id"]
            row["sort_order"] = sort_order
            itinerary_rows.append(row)
        itinerary_result = client.table("itinerary_items").insert(itinerary_rows).execute()
        if len(itinerary_result.data or []) != len(itinerary_rows):
            raise RuntimeError("AI 일정 행을 모두 저장하지 못했습니다.")

    except Exception as error:
        if trip and trip.get("id"):
            _remove_incomplete_trip(client, trip["id"], current_user.id)
        LOGGER.error("새 AI 여행 저장 실패 (%s).", type(error).__name__)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="여행과 AI 일정을 저장하지 못했습니다. 다시 시도해 주세요.",
        ) from error

    dashboard = trip_dashboard(client, trip["id"])
    dashboard["initial_itinerary_count"] = len(resolved_drafts)
    record_activity(
        client,
        user_id=current_user.id,
        event_type="trip.create",
        trip_id=trip["id"],
        entity_type="trip",
        entity_id=trip["id"],
        metadata={"destination": trip.get("destination"), "day_count": len(days)},
    )
    return dashboard


@router.get(
    "/trips/{trip_id}/dashboard", summary="여행 대시보드 조회",
    response_description="여행 기본 정보, DAY별 일정, 연결된 장소 정보",
    responses={401: {"description": "로그인이 필요함"}, 404: {"description": "여행을 찾을 수 없거나 접근 권한이 없음"}},
)
def read_trip_dashboard(
    trip_id: UUID,
    current_user: CurrentUser = Depends(get_current_user),
):
    """접근 가능한 여행 하나와 모든 DAY·일정 항목을 함께 반환한다."""

    return trip_dashboard(get_user_client(current_user.token), trip_id)


@router.patch("/trips/{trip_id}/pin", summary="여행 고정 상태 변경")
def update_trip_pin(
    trip_id: UUID,
    payload: TripPinUpdate,
    current_user: CurrentUser = Depends(get_current_user),
):
    """소유자의 고정 순서가 끊기지 않도록 여행 하나를 고정하거나 해제한다.

    고정된 여행은 0부터 시작하는 ``pinned_order`` 값을 가진다. ``NULL``은
    고정되지 않았다는 뜻이다. 이미 고정되었거나 해제된 여행에 같은 요청을
    다시 보내도 현재 상태가 유지되므로 안전하다.
    """

    client = get_user_client(current_user.token)
    trip = _owned_trip(client, trip_id)
    is_currently_pinned = trip.get("pinned_order") is not None

    if payload.pinned and not is_currently_pinned:
        current_pins = _pinned_trips_for_user(client, current_user.id)
        # 이전 수동 수정으로 pinned_order에 빈 자리가 남았을 때 새로 추가한 여행이
        # 오래된 값을 재사용하지 않도록 먼저 기존 데이터를 정리한다.
        _normalize_pinned_orders(client, current_user.id, current_pins)
        result = (
            client.table("trips")
            .update({"pinned_order": len(current_pins)})
            .eq("id", str(trip_id))
            .eq("user_id", current_user.id)
            .execute()
        )
        if not result.data:
            raise HTTPException(status_code=404, detail="여행을 찾을 수 없습니다.")
    elif not payload.pinned and is_currently_pinned:
        result = (
            client.table("trips")
            .update({"pinned_order": None})
            .eq("id", str(trip_id))
            .eq("user_id", current_user.id)
            .execute()
        )
        if not result.data:
            raise HTTPException(status_code=404, detail="여행을 찾을 수 없습니다.")
        # 대상값을 비운 뒤 조회하면 이후 모든 고정 여행이 앞으로 한 칸씩 이동해,
        # 저장된 순서가 다시 0부터 n-1까지가 된다.
        _normalize_pinned_orders(
            client,
            current_user.id,
            _pinned_trips_for_user(client, current_user.id),
        )

    record_activity(
        client,
        user_id=current_user.id,
        event_type="trip.pin" if payload.pinned else "trip.unpin",
        trip_id=trip_id,
        entity_type="trip",
        entity_id=trip_id,
        metadata={"pinned": payload.pinned},
    )
    return _owned_trip(client, trip_id)


@router.patch("/trips/{trip_id}", summary="여행 기본 정보 수정")
def update_trip(
    trip_id: UUID,
    payload: TripUpdate,
    current_user: CurrentUser = Depends(get_current_user),
):
    """여행 기간 외의 정보를 수정하고 갱신된 대시보드 데이터를 반환한다."""

    client = get_user_client(current_user.token)
    _owned_trip(client, trip_id)
    values = payload.model_dump(exclude_unset=True, mode="json")
    if "start_date" in values or "end_date" in values:
        raise HTTPException(
            status_code=400,
            detail="여행 기간은 전용 날짜 변경 API로 수정하세요.",
        )
    if not values:
        return trip_dashboard(client, trip_id)
    values["updated_at"] = datetime.now(timezone.utc).isoformat()
    result = client.table("trips").update(values).eq("id", str(trip_id)).execute()
    if not result.data:
        raise HTTPException(status_code=404, detail="여행을 찾을 수 없습니다.")
    return trip_dashboard(client, trip_id)


@router.patch("/trips/{trip_id}/dates", summary="여행 날짜 변경")
def update_trip_dates(
    trip_id: UUID,
    payload: TripDateRangeUpdate,
    current_user: CurrentUser = Depends(get_current_user),
):
    """여행 기간을 수정하고 DAY 행과 일정 날짜를 함께 동기화한다."""

    client = get_user_client(current_user.token)
    # 아래의 여러 테이블 동기화를 수행하기 전에 접근 권한을 확인한다.
    trip = _owned_trip(client, trip_id)
    _sync_trip_days_for_dates(
        client,
        trip_id,
        payload.start_date,
        payload.end_date,
        trip["timezone"],
    )
    result = (
        client.table("trips")
        .update(
            {
                "start_date": payload.start_date.isoformat(),
                "end_date": payload.end_date.isoformat(),
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }
        )
        .eq("id", str(trip_id))
        .execute()
    )
    if not result.data:
        raise HTTPException(status_code=404, detail="여행을 찾을 수 없습니다.")
    record_activity(
        client,
        user_id=current_user.id,
        event_type="trip.dates_update",
        trip_id=trip_id,
        entity_type="trip",
        entity_id=trip_id,
        metadata={
            "start_date": payload.start_date.isoformat(),
            "end_date": payload.end_date.isoformat(),
        },
    )
    return trip_dashboard(client, trip_id)


@router.delete("/trips/{trip_id}", status_code=status.HTTP_204_NO_CONTENT, summary="여행 삭제")
def delete_trip(
    trip_id: UUID,
    current_user: CurrentUser = Depends(get_current_user),
):
    """접근 가능한 여행 하나를 삭제하며, 연결된 데이터는 DB 외래 키가 처리한다."""

    client = get_user_client(current_user.token)
    _owned_trip(client, trip_id)
    record_activity(
        client,
        user_id=current_user.id,
        event_type="trip.delete",
        trip_id=trip_id,
        entity_type="trip",
        entity_id=trip_id,
    )
    client.table("trips").delete().eq("id", str(trip_id)).execute()


@router.post("/trips/{trip_id}/days", status_code=status.HTTP_201_CREATED, summary="여행 일차 추가")
def create_trip_day(
    trip_id: UUID,
    payload: TripDayCreate,
    current_user: CurrentUser = Depends(get_current_user),
):
    """접근 가능한 여행에 DAY 행 하나를 추가한다."""

    client = get_user_client(current_user.token)
    _owned_trip(client, trip_id)
    values = payload.model_dump(mode="json")
    values["trip_id"] = str(trip_id)
    result = client.table("trip_days").insert(values).execute()
    touch_trip(client, trip_id)
    record_activity(
        client,
        user_id=current_user.id,
        event_type="itinerary.day_create",
        trip_id=trip_id,
        entity_type="trip_day",
        entity_id=result.data[0].get("id") if result.data else None,
        metadata={"day_number": values.get("day_number")},
    )
    return result.data[0]


@router.post("/trips/{trip_id}/itinerary-items", status_code=status.HTTP_201_CREATED, summary="일정 항목 추가")
def create_itinerary_item(
    trip_id: UUID,
    payload: ItineraryItemCreate,
    current_user: CurrentUser = Depends(get_current_user),
):
    """접근 가능한 여행과 선택한 DAY에 일정 항목 하나를 마지막에 추가한다."""

    client = get_user_client(current_user.token)
    _owned_trip(client, trip_id)
    if payload.trip_day_id:
        _owned_day(client, trip_id, payload.trip_day_id)

    values = payload.model_dump(exclude_none=True, mode="json")
    values["trip_id"] = str(trip_id)
    last_item = (
        client.table("itinerary_items")
        .select("sort_order")
        .eq("trip_id", str(trip_id))
        .order("sort_order", desc=True)
        .limit(1)
        .execute()
        .data
    )
    # 클라이언트가 정렬값을 보내지 않으면 새 항목은 현재 마지막 항목 뒤에 둔다.
    values["sort_order"] = (last_item[0]["sort_order"] + 1) if last_item else 0
    result = client.table("itinerary_items").insert(values).execute()
    if not result.data:
        raise HTTPException(status_code=400, detail="일정을 추가하지 못했습니다.")
    touch_trip(client, trip_id)
    record_activity(
        client,
        user_id=current_user.id,
        event_type="itinerary.item_create",
        trip_id=trip_id,
        entity_type="itinerary_item",
        entity_id=result.data[0].get("id"),
        metadata={"item_type": values.get("item_type")},
    )
    return result.data[0]


@router.patch("/trips/{trip_id}/itinerary-items/{item_id}", summary="일정 항목 수정")
def update_itinerary_item(
    trip_id: UUID,
    item_id: UUID,
    payload: ItineraryItemUpdate,
    current_user: CurrentUser = Depends(get_current_user),
):
    """접근 가능한 일정 하나를 수정하고, DAY를 옮기면 소속 여부도 확인한다."""

    client = get_user_client(current_user.token)
    _owned_trip(client, trip_id)
    existing = (
        client.table("itinerary_items")
        .select("*")
        .eq("id", str(item_id))
        .eq("trip_id", str(trip_id))
        .execute()
    )
    if not existing.data:
        raise HTTPException(status_code=404, detail="일정을 찾을 수 없습니다.")

    values = payload.model_dump(exclude_unset=True, mode="json")
    if "trip_day_id" in values and values["trip_day_id"] is not None:
        _owned_day(client, trip_id, values["trip_day_id"])
    if not values:
        return existing.data[0]
    result = client.table("itinerary_items").update(values).eq("id", str(item_id)).execute()
    touch_trip(client, trip_id)
    record_activity(
        client,
        user_id=current_user.id,
        event_type="itinerary.item_update",
        trip_id=trip_id,
        entity_type="itinerary_item",
        entity_id=item_id,
        metadata={"fields": sorted(values)},
    )
    return result.data[0]


@router.post("/trips/{trip_id}/itinerary-items/{item_id}/time", summary="일정 시간 변경")
def update_itinerary_item_time(
    trip_id: UUID,
    item_id: UUID,
    payload: ItineraryItemTimeUpdate,
    current_user: CurrentUser = Depends(get_current_user),
):
    """한 일정 칸의 시작·종료 시각을 직접 바꾸고 되돌리기 로그를 남긴다."""

    client = get_user_client(current_user.token)
    trip = _owned_trip(client, trip_id)
    existing_result = (
        client.table("itinerary_items")
        .select("*")
        .eq("id", str(item_id))
        .eq("trip_id", str(trip_id))
        .execute()
    )
    if not existing_result.data:
        raise HTTPException(status_code=404, detail="일정을 찾을 수 없습니다.")
    existing = existing_result.data[0]
    if not existing.get("trip_day_id"):
        raise HTTPException(status_code=400, detail="DAY에 연결되지 않은 일정은 시간을 변경할 수 없습니다.")
    day = _owned_day(client, trip_id, existing["trip_day_id"])

    start_at = _local_datetime_for_day(day, payload.start_time, trip.get("timezone"))
    end_at = _local_datetime_for_day(day, payload.end_time, trip.get("timezone"))
    if end_at <= start_at:
        raise HTTPException(status_code=422, detail="종료 시간은 시작 시간보다 늦어야 합니다.")
    values = {
        "start_at": start_at.isoformat(),
        "end_at": end_at.isoformat(),
        "estimated_stay_minutes": int((end_at - start_at).total_seconds() // 60),
        # 사용자가 직접 고른 시간은 Routes의 표시용 자동 보정에 밀리지 않는다.
        "is_fixed": True,
    }
    before_data = _change_data([existing])
    try:
        result = (
            client.table("itinerary_items")
            .update(values)
            .eq("id", str(item_id))
            .eq("trip_id", str(trip_id))
            .execute()
        )
        if not result.data:
            raise RuntimeError("수정된 일정 행을 반환받지 못했습니다.")
        changed = result.data[0]
        change_log = _record_itinerary_change(
            client,
            trip_id,
            action_type="time_changed",
            actor_type="user",
            before_data=before_data,
            after_data=_change_data(
                [changed],
                {
                    "message": (
                        f"DAY {day['day_number']}의 {changed.get('title') or '일정'} 시간이 변경되었습니다."
                    ),
                    "detail": (
                        f"{_clock_text(existing.get('start_at'), trip.get('timezone'))}"
                        f"–{_clock_text(existing.get('end_at'), trip.get('timezone'))}"
                        f" → {_clock_text(changed.get('start_at'), trip.get('timezone'))}"
                        f"–{_clock_text(changed.get('end_at'), trip.get('timezone'))}"
                    ),
                },
            ),
        )
    except Exception as error:
        # 로그 저장까지 성공해야 사용자가 Undo할 수 있다. 기록이 실패하면 시간도
        # 원래대로 돌려 "바뀌었지만 되돌릴 수 없는" 상태를 남기지 않는다.
        try:
            client.table("itinerary_items").update(
                {
                    field: existing.get(field)
                    for field in ("start_at", "end_at", "estimated_stay_minutes", "is_fixed")
                }
            ).eq("id", str(item_id)).eq("trip_id", str(trip_id)).execute()
        except Exception:
            LOGGER.error("실패한 일정 시간 변경 복구에 실패했습니다.")
        if isinstance(error, HTTPException):
            raise error
        LOGGER.warning("일정 시간 변경 실패 (%s).", type(error).__name__)
        raise HTTPException(status_code=500, detail="일정 시간을 변경하지 못했습니다.") from error

    touch_trip(client, trip_id)
    record_activity(
        client,
        user_id=current_user.id,
        event_type="itinerary.time_change",
        trip_id=trip_id,
        entity_type="itinerary_item",
        entity_id=item_id,
        metadata={
            "start_time": payload.start_time.isoformat(),
            "end_time": payload.end_time.isoformat(),
        },
    )
    return {"item": changed, "change": _change_status(change_log, trip.get("timezone"))}


def apply_ai_place_swap(
    client,
    trip_id: UUID | str,
    first_item_id: UUID | str,
    second_item_id: UUID | str,
) -> dict:
    """AI가 확인한 같은 DAY의 두 일정 시간 칸에서 장소 정보만 교환한다.

    자연어 해석 결과는 이 함수에 도달하기 전에 후보 ID 형식만 검증된 값이다.
    여기서는 현재 사용자 권한, 두 일정의 실제 소속과 DAY 일치를 다시 확인한다.
    따라서 모델 출력이 잘못되거나 오래된 채팅 요청이 와도 다른 여행·다른 DAY의
    일정 또는 시간 자체를 바꾸지 않는다.
    """

    trip = _owned_trip(client, trip_id)
    first_result = (
        client.table("itinerary_items")
        .select("*")
        .eq("id", str(first_item_id))
        .eq("trip_id", str(trip_id))
        .execute()
    )
    second_result = (
        client.table("itinerary_items")
        .select("*")
        .eq("id", str(second_item_id))
        .eq("trip_id", str(trip_id))
        .execute()
    )
    if not first_result.data or not second_result.data:
        raise HTTPException(status_code=404, detail="바꿀 일정 중 하나를 찾을 수 없습니다.")
    first_item, second_item = first_result.data[0], second_result.data[0]
    first_day_id, second_day_id = first_item.get("trip_day_id"), second_item.get("trip_day_id")
    if not first_day_id or not second_day_id or str(first_day_id) != str(second_day_id):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="AI는 현재 같은 DAY 안의 일정 두 개만 순서를 바꿀 수 있습니다.",
        )
    day = _owned_day(client, trip_id, first_day_id)

    before_data = _change_data([first_item, second_item])
    first_values = {field: second_item.get(field) for field in _PLACE_SLOT_FIELDS}
    second_values = {field: first_item.get(field) for field in _PLACE_SLOT_FIELDS}
    try:
        changed_first = (
            client.table("itinerary_items")
            .update(first_values)
            .eq("id", str(first_item["id"]))
            .eq("trip_id", str(trip_id))
            .execute()
        )
        changed_second = (
            client.table("itinerary_items")
            .update(second_values)
            .eq("id", str(second_item["id"]))
            .eq("trip_id", str(trip_id))
            .execute()
        )
        if not changed_first.data or not changed_second.data:
            raise RuntimeError("AI가 교환한 일정 행을 반환받지 못했습니다.")
        changed_items = [changed_first.data[0], changed_second.data[0]]
        change_log = _record_itinerary_change(
            client,
            trip_id,
            action_type="place_swapped",
            actor_type="ai",
            before_data=before_data,
            after_data=_change_data(
                changed_items,
                {
                    "message": f"AI가 DAY {day['day_number']}의 일정 위치를 바꿨어요.",
                    "detail": (
                        f"{_clock_text(first_item.get('start_at'), trip.get('timezone'))} "
                        f"{first_item.get('title') or '일정'}"
                        f" ↔ {_clock_text(second_item.get('start_at'), trip.get('timezone'))} "
                        f"{second_item.get('title') or '일정'}"
                    ),
                },
            ),
        )
    except Exception as error:
        # 두 update와 로그 기록을 하나의 변경으로 취급한다. 중간 실패면 장소 정보도
        # 원상 복구해 시간 칸 두 개가 반만 교환되는 상태를 남기지 않는다.
        try:
            client.table("itinerary_items").update(
                {field: first_item.get(field) for field in _PLACE_SLOT_FIELDS}
            ).eq("id", str(first_item["id"])).eq("trip_id", str(trip_id)).execute()
            client.table("itinerary_items").update(
                {field: second_item.get(field) for field in _PLACE_SLOT_FIELDS}
            ).eq("id", str(second_item["id"])).eq("trip_id", str(trip_id)).execute()
        except Exception:
            LOGGER.error("실패한 AI 일정 장소 교환 복구에 실패했습니다.")
        if isinstance(error, HTTPException):
            raise error
        LOGGER.warning("AI 일정 장소 교환 실패 (%s).", type(error).__name__)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="AI가 일정 순서를 바꾸지 못했습니다.",
        ) from error

    touch_trip(client, trip_id)
    return {"items": changed_items, "change": _change_status(change_log, trip.get("timezone"))}


def apply_ai_time_change(
    client,
    trip_id: UUID | str,
    item_id: UUID | str,
    new_start_time: time,
) -> dict:
    """AI가 명확히 식별한 한 일정의 시간을 실제 DB와 변경 로그에 함께 저장한다."""

    trip = _owned_trip(client, trip_id)
    existing_result = (
        client.table("itinerary_items").select("*").eq("id", str(item_id))
        .eq("trip_id", str(trip_id)).execute()
    )
    if not existing_result.data:
        raise HTTPException(status_code=404, detail="시간을 바꿀 일정을 찾을 수 없습니다.")
    existing = existing_result.data[0]
    if not existing.get("trip_day_id"):
        raise HTTPException(status_code=422, detail="DAY에 연결되지 않은 일정은 시간을 바꿀 수 없습니다.")
    day = _owned_day(client, trip_id, existing["trip_day_id"])
    old_start = _local_datetime_for_day(day, time(0, 0), trip.get("timezone"))
    try:
        old_start = datetime.fromisoformat(str(existing["start_at"]).replace("Z", "+00:00"))
        old_end = datetime.fromisoformat(str(existing["end_at"]).replace("Z", "+00:00"))
        duration = old_end - old_start
        if duration <= timedelta(0):
            raise ValueError
    except (KeyError, TypeError, ValueError):
        duration = timedelta(minutes=60)
    new_start = _local_datetime_for_day(day, new_start_time, trip.get("timezone"))
    new_end = new_start + duration
    before_data = _change_data([existing])
    values = {
        "start_at": new_start.isoformat(),
        "end_at": new_end.isoformat(),
        "estimated_stay_minutes": int(duration.total_seconds() // 60),
        "is_fixed": True,
    }
    try:
        result = client.table("itinerary_items").update(values).eq("id", str(item_id)).eq(
            "trip_id", str(trip_id)
        ).execute()
        if not result.data:
            raise RuntimeError("AI가 수정한 일정 행을 반환받지 못했습니다.")
        changed = result.data[0]
        change_log = _record_itinerary_change(
            client, trip_id, action_type="time_changed", actor_type="ai",
            before_data=before_data,
            after_data=_change_data([changed], {"message": f"AI가 DAY {day['day_number']}의 {changed.get('title') or '일정'} 시간을 변경했어요."}),
        )
    except Exception as error:
        if isinstance(error, HTTPException):
            raise error
        LOGGER.warning("AI 일정 시간 변경 실패 (%s).", type(error).__name__)
        raise HTTPException(status_code=500, detail="AI가 일정 시간을 바꾸지 못했습니다.") from error
    touch_trip(client, trip_id)
    return {"item": changed, "change": _change_status(change_log, trip.get("timezone"))}


@router.post("/trips/{trip_id}/itinerary-items/{item_id}/swap-place", summary="일정 장소 교체")
def swap_itinerary_item_place(
    trip_id: UUID,
    item_id: UUID,
    payload: ItineraryPlaceSwap,
    current_user: CurrentUser = Depends(get_current_user),
):
    """일정 칸의 장소를 바로 앞 또는 뒤 시간 칸의 장소와 교환한다."""

    client = get_user_client(current_user.token)
    trip = _owned_trip(client, trip_id)
    item_result = (
        client.table("itinerary_items")
        .select("*")
        .eq("id", str(item_id))
        .eq("trip_id", str(trip_id))
        .execute()
    )
    if not item_result.data:
        raise HTTPException(status_code=404, detail="일정을 찾을 수 없습니다.")
    current_item = item_result.data[0]
    if not current_item.get("trip_day_id"):
        raise HTTPException(status_code=400, detail="DAY에 연결되지 않은 일정은 순서를 바꿀 수 없습니다.")
    day = _owned_day(client, trip_id, current_item["trip_day_id"])
    ordered_items = _ordered_day_items(client, trip_id, day["id"])
    try:
        current_index = next(
            index for index, item in enumerate(ordered_items) if str(item["id"]) == str(item_id)
        )
    except StopIteration as error:
        raise HTTPException(status_code=404, detail="일정을 찾을 수 없습니다.") from error

    target_index = current_index - 1 if payload.direction == "previous" else current_index + 1
    if target_index < 0 or target_index >= len(ordered_items):
        edge_text = "첫 번째" if payload.direction == "previous" else "마지막"
        raise HTTPException(status_code=409, detail=f"이미 {edge_text} 일정입니다.")
    target_item = ordered_items[target_index]

    before_data = _change_data([current_item, target_item])
    current_values = {field: target_item.get(field) for field in _PLACE_SLOT_FIELDS}
    target_values = {field: current_item.get(field) for field in _PLACE_SLOT_FIELDS}
    try:
        changed_current = (
            client.table("itinerary_items")
            .update(current_values)
            .eq("id", str(current_item["id"]))
            .eq("trip_id", str(trip_id))
            .execute()
        )
        changed_target = (
            client.table("itinerary_items")
            .update(target_values)
            .eq("id", str(target_item["id"]))
            .eq("trip_id", str(trip_id))
            .execute()
        )
        if not changed_current.data or not changed_target.data:
            raise RuntimeError("교환된 일정 행을 반환받지 못했습니다.")
        changed_items = [changed_current.data[0], changed_target.data[0]]
        change_log = _record_itinerary_change(
            client,
            trip_id,
            action_type="place_swapped",
            actor_type="user",
            before_data=before_data,
            after_data=_change_data(
                changed_items,
                {
                    "message": f"DAY {day['day_number']}의 일정 순서가 변경되었습니다.",
                    "detail": (
                        f"{_clock_text(current_item.get('start_at'), trip.get('timezone'))} "
                        f"{current_item.get('title') or '일정'}"
                        f" ↔ {_clock_text(target_item.get('start_at'), trip.get('timezone'))} "
                        f"{target_item.get('title') or '일정'}"
                    ),
                },
            ),
        )
    except Exception as error:
        # 두 행 중 하나만 바뀐 상태를 남기지 않도록 실패 시 원래 장소 정보를 되돌린다.
        try:
            client.table("itinerary_items").update(
                {field: current_item.get(field) for field in _PLACE_SLOT_FIELDS}
            ).eq("id", str(current_item["id"])).eq("trip_id", str(trip_id)).execute()
            client.table("itinerary_items").update(
                {field: target_item.get(field) for field in _PLACE_SLOT_FIELDS}
            ).eq("id", str(target_item["id"])).eq("trip_id", str(trip_id)).execute()
        except Exception:
            LOGGER.error("실패한 일정 장소 교환 복구에 실패했습니다.")
        if isinstance(error, HTTPException):
            raise error
        LOGGER.warning("일정 장소 교환 실패 (%s).", type(error).__name__)
        raise HTTPException(status_code=500, detail="일정 순서를 변경하지 못했습니다.") from error

    touch_trip(client, trip_id)
    record_activity(
        client,
        user_id=current_user.id,
        event_type="itinerary.place_swap",
        trip_id=trip_id,
        entity_type="itinerary_item",
        entity_id=item_id,
        metadata={"direction": payload.direction},
    )
    return {"items": changed_items, "change": _change_status(change_log, trip.get("timezone"))}


@router.get("/trips/{trip_id}/itinerary-changes", summary="일정 변경 이력 조회")
def list_itinerary_changes(
    trip_id: UUID,
    current_user: CurrentUser = Depends(get_current_user),
):
    """채팅 타임라인에 섞어 표시할 모든 일정 변경 상태를 시간순으로 반환한다."""

    client = get_user_client(current_user.token)
    trip = _owned_trip(client, trip_id)
    result = (
        client.table("itinerary_change_logs")
        .select("*")
        .eq("trip_id", str(trip_id))
        .order("created_at")
        .execute()
    )
    return [_change_status(change, trip.get("timezone")) for change in (result.data or [])]


def _undo_fields(action_type: str) -> tuple[str, ...]:
    """변경 종류별로 Undo가 복원해야 할 일정 필드만 반환한다."""

    if action_type == "time_changed":
        return ("start_at", "end_at", "estimated_stay_minutes", "is_fixed")
    if action_type == "place_swapped":
        return _PLACE_SLOT_FIELDS
    raise HTTPException(status_code=422, detail="이 변경은 아직 버튼으로 되돌릴 수 없습니다.")


def _undo_values_match(field: str, current_value: object, saved_value: object) -> bool:
    """현재 값이 해당 변경 직후 값과 같은지 비교해 선택 Undo의 안전성을 확인한다."""

    if field in {"start_at", "end_at"}:
        try:
            current_time = datetime.fromisoformat(str(current_value).replace("Z", "+00:00"))
            saved_time = datetime.fromisoformat(str(saved_value).replace("Z", "+00:00"))
            return current_time == saved_time
        except (TypeError, ValueError):
            # 둘 다 null인 경우처럼 문자열 변환 비교가 더 정확한 값도 허용한다.
            return current_value == saved_value
    return current_value == saved_value


@router.post("/trips/{trip_id}/itinerary-changes/{log_id}/undo", summary="일정 변경 되돌리기")
def undo_itinerary_change(
    trip_id: UUID,
    log_id: UUID,
    current_user: CurrentUser = Depends(get_current_user),
):
    """선택한 일정 변경 한 건을 안전할 때만 로그의 변경 전 값으로 되돌린다."""

    client = get_user_client(current_user.token)
    _owned_trip(client, trip_id)
    log_result = (
        client.table("itinerary_change_logs")
        .select("*")
        .eq("id", str(log_id))
        .eq("trip_id", str(trip_id))
        .execute()
    )
    if not log_result.data:
        raise HTTPException(status_code=404, detail="되돌릴 일정 변경 기록을 찾을 수 없습니다.")
    source_log = log_result.data[0]
    if source_log.get("is_reverted") or source_log.get("action_type") == "undo":
        raise HTTPException(status_code=409, detail="이미 되돌렸거나 되돌릴 수 없는 변경입니다.")

    before_data = source_log.get("before_data") if isinstance(source_log.get("before_data"), dict) else {}
    snapshots = before_data.get("items") if isinstance(before_data.get("items"), list) else []
    if not snapshots:
        raise HTTPException(status_code=422, detail="이 변경에는 복원할 이전 일정 정보가 없습니다.")
    fields = _undo_fields(str(source_log.get("action_type") or ""))
    after_data = source_log.get("after_data") if isinstance(source_log.get("after_data"), dict) else {}
    after_snapshots = after_data.get("items") if isinstance(after_data.get("items"), list) else []
    after_by_id = {
        str(item.get("id")): item
        for item in after_snapshots
        if isinstance(item, dict) and item.get("id")
    }

    current_items: list[dict] = []
    try:
        for snapshot in snapshots:
            if not isinstance(snapshot, dict) or not snapshot.get("id"):
                raise RuntimeError("로그의 일정 ID가 올바르지 않습니다.")
            existing = (
                client.table("itinerary_items")
                .select("*")
                .eq("id", str(snapshot["id"]))
                .eq("trip_id", str(trip_id))
                .execute()
            )
            if not existing.data:
                raise HTTPException(status_code=409, detail="변경 뒤 삭제된 일정이 있어 되돌릴 수 없습니다.")
            current_item = existing.data[0]
            after_item = after_by_id.get(str(snapshot["id"]))
            if after_item is None:
                raise HTTPException(status_code=422, detail="이 변경에는 비교할 이후 일정 정보가 없습니다.")
            # 시간 변경 뒤 장소 순서를 바꾼 것처럼 서로 다른 필드의 수정은 허용한다.
            # 반대로 같은 필드가 다시 수정됐다면 이 옛 변경을 복원하면 최신 값이
            # 사라지므로, 먼저 그 최신 변경을 처리하도록 안내한다.
            if any(
                not _undo_values_match(field, current_item.get(field), after_item.get(field))
                for field in fields
            ):
                raise HTTPException(
                    status_code=409,
                    detail="이 변경 뒤 같은 일정 정보가 다시 수정되어 안전하게 되돌릴 수 없습니다. 해당 최신 변경을 먼저 되돌려 주세요.",
                )
            current_items.append(current_item)

        for snapshot in snapshots:
            values = {field: snapshot.get(field) for field in fields}
            result = (
                client.table("itinerary_items")
                .update(values)
                .eq("id", str(snapshot["id"]))
                .eq("trip_id", str(trip_id))
                .execute()
            )
            if not result.data:
                raise RuntimeError("복원된 일정 행을 반환받지 못했습니다.")

        # 원본을 먼저 되돌림 완료로 표시한다. 그 다음 Undo 행 저장이 실패하면
        # 아래 복구 처리에서 이 표시도 다시 해제한다.
        reverted = (
            client.table("itinerary_change_logs")
            .update({"is_reverted": True, "reverted_at": datetime.now(timezone.utc).isoformat()})
            .eq("id", str(source_log["id"]))
            .eq("trip_id", str(trip_id))
            .execute()
        )
        if not reverted.data:
            raise RuntimeError("원본 일정 변경 기록을 갱신하지 못했습니다.")
        undo_log = _record_itinerary_change(
            client,
            trip_id,
            action_type="undo",
            actor_type="user",
            before_data=_change_data(current_items),
            after_data={
                "items": snapshots,
                "summary": {
                    "message": "변경 취소하였습니다.",
                    "detail": "선택한 일정 변경을 이전 상태로 복원했습니다.",
                },
            },
            undo_of_log_id=source_log["id"],
        )
    except Exception as error:
        # 여러 행의 복원·로그 저장은 한 묶음이다. 로그 저장 또는 표시 상태 갱신에
        # 실패하면, 이미 복원한 행도 다시 변경 직전 값으로 돌려 불완전한 Undo를
        # 남기지 않는다.
        try:
            for current_item in current_items:
                client.table("itinerary_items").update(
                    {field: current_item.get(field) for field in fields}
                ).eq("id", str(current_item["id"])).eq("trip_id", str(trip_id)).execute()
        except Exception:
            LOGGER.error("실패한 일정 변경 Undo 복구에 실패했습니다.")
        try:
            client.table("itinerary_change_logs").update(
                {"is_reverted": False, "reverted_at": None}
            ).eq("id", str(source_log["id"])).eq("trip_id", str(trip_id)).execute()
        except Exception:
            LOGGER.error("실패한 일정 변경 Undo 로그 복구에 실패했습니다.")
        if isinstance(error, HTTPException):
            raise error
        LOGGER.warning("일정 변경 Undo 실패 (%s).", type(error).__name__)
        raise HTTPException(status_code=500, detail="일정 변경을 취소하지 못했습니다.") from error

    touch_trip(client, trip_id)
    record_activity(
        client,
        user_id=current_user.id,
        event_type="itinerary.undo",
        trip_id=trip_id,
        entity_type="itinerary_change_log",
        entity_id=log_id,
        metadata={"source_log_id": str(source_log["id"])},
    )
    return {"change": _change_status(undo_log, trip.get("timezone"))}


@router.delete(
    "/trips/{trip_id}/itinerary-items/{item_id}", status_code=status.HTTP_204_NO_CONTENT,
    summary="일정 항목 삭제",
)
def delete_itinerary_item(
    trip_id: UUID,
    item_id: UUID,
    current_user: CurrentUser = Depends(get_current_user),
):
    """접근 가능한 여행에 속한 일정 항목 하나만 삭제한다."""

    client = get_user_client(current_user.token)
    _owned_trip(client, trip_id)
    result = (
        client.table("itinerary_items")
        .delete()
        .eq("id", str(item_id))
        .eq("trip_id", str(trip_id))
        .execute()
    )
    if not result.data:
        raise HTTPException(status_code=404, detail="일정을 찾을 수 없습니다.")
    touch_trip(client, trip_id)
    record_activity(
        client,
        user_id=current_user.id,
        event_type="itinerary.item_delete",
        trip_id=trip_id,
        entity_type="itinerary_item",
        entity_id=item_id,
    )

# =============================================================================
# 일정표 이미지 다운로드 (SCR-007)
#
# 스타일 선택 -> 다운로드 두 단계로 끝난다. 심플형과 일러스트형 모두 LLM 이
# 그리며, 스타일은 프롬프트만 바꾼다 (services/itinerary_export_prompt.py).
# =============================================================================


def _itinerary_export_zip(trip: dict, images: list[bytes | None]) -> Response:
    """장별 PNG 를 ZIP 한 개로 묶어 내려보낸다.

    **ZIP 은 전송 형식일 뿐이다.** 사용자는 ZIP 을 저장하지 않는다 - 화면이 풀어서
    장별 [저장] 버튼을 보여 준다. 장마다 따로 내려받게 하면 8일 여행에서 화면이
    네 번을 순차로 기다려 타임아웃이 먼저 난다.

    압축하지 않는다(ZIP_STORED). PNG 는 이미 압축돼 있어 다시 압축해도 크기가
    거의 안 줄고 시간만 든다.

    HTTP 헤더는 latin-1 만 담을 수 있어 한글 파일명을 그대로 넣으면 응답 자체가
    터진다. RFC 5987 로 UTF-8 이름을 주고, 못 읽는 클라이언트를 위해 ASCII 이름도
    함께 둔다.
    """

    import io
    import zipfile
    from urllib.parse import quote

    stamp = str(trip.get("start_date") or "").replace("-", "") or "undated"
    place = str(trip.get("destination") or "trip")
    total = len(images)

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_STORED) as archive:
        for page, png in enumerate(images, start=1):
            # 실패한 장은 건너뛰되 **페이지 번호는 유지한다.** 번호를 다시 매기면
            # 파일 이름의 2of4 가 실제 순서와 어긋난다.
            if png is None:
                continue
            archive.writestr(f"tripmate_{place}_{stamp}_{page}of{total}.png", png)

    korean = f"tripmate_{place}_{stamp}.zip"
    ascii_name = f"tripmate_{stamp}.zip"
    return Response(
        content=buffer.getvalue(),
        media_type="application/zip",
        headers={
            "Content-Disposition":
                f'attachment; filename="{ascii_name}"; '
                f"filename*=UTF-8''{quote(korean)}",
            # 화면이 받은 장수와 기대 장수를 견줄 수 있게 알려 준다.
            "X-Total-Pages": str(total),
        },
    )


@router.get("/trips/{trip_id}/itinerary/export", summary="일정 이미지로 내보내기")
def export_itinerary_image(
    trip_id: UUID,
    # 기본값을 심플형으로 둔다. 인쇄용이 더 자주 쓰이고, 시안에서도 심플형이
    # [기본 선택] 이다. Literal 이라 잘못된 값은 FastAPI 가 422 로 먼저 막는다.
    style: Literal["simple", "illustrated"] = "simple",
    current_user: CurrentUser = Depends(get_current_user),
):
    """저장된 일정을 일정표 PNG 로 그려 ZIP 으로 내려보낸다.

    일정이 길면 여러 장으로 나눠 그린다 (services/itinerary_export.DAYS_PER_PAGE).
    한 장짜리도 같은 ZIP 으로 보낸다 - 화면이 한 갈래로만 처리하게 하려는 것이다.
    """

    client = get_user_client(current_user.token)
    # 소유권 확인과 trip/days 로딩을 대시보드와 같은 경로로 처리한다. 여기서
    # 따로 조회하면 그림이 화면과 다른 일정을 그릴 여지가 생긴다.
    dashboard = trip_dashboard(client, trip_id)
    trip, days = dashboard["trip"], dashboard["days"]
    if not any(day.get("items") for day in days):
        raise HTTPException(status_code=404, detail="내보낼 일정이 없습니다.")

    images = generate_itinerary_images(trip, days, style)
    if not any(images):
        # 한 장도 못 그렸다. 일정 자체는 있으므로 /export/text 로 붙여넣을 수 있는
        # 텍스트를 받을 수 있다고 알린다.
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="지금은 일정표 이미지를 만들 수 없습니다. 일정 텍스트로 저장해 주세요.",
        )

    drawn = [png for png in images if png]
    record_activity(
        client,
        user_id=current_user.id,
        event_type="itinerary.export",
        trip_id=trip_id,
        # metadata 키 이름 주의 - routers/console.py 가 rating/sentiment/feedback/
        # pace/intensity/travel_intensity 키를 보고 피드백·페이스 로그로 분류한다.
        # 그 이름을 쓰면 이 로그가 운영 콘솔 집계에 잘못 섞인다.
        metadata={
            "style": style,
            "format": "zip",
            "pages": len(images),
            "drawn_pages": len(drawn),
            "size_bytes": sum(len(png) for png in drawn),
        },
    )
    return _itinerary_export_zip(trip, images)


@router.get("/trips/{trip_id}/itinerary/export/text", summary="일정 텍스트로 내보내기")
def export_itinerary_text(
    trip_id: UUID,
    current_user: CurrentUser = Depends(get_current_user),
):
    client = get_user_client(current_user.token)
    dashboard = trip_dashboard(client, trip_id)
    trip, days = dashboard["trip"], dashboard["days"]
    if not any(day.get("items") for day in days):
        raise HTTPException(status_code=404, detail="내보낼 일정이 없습니다.")
    return {"text": itinerary_as_text(trip, days)}
