"""보호된 Google 장소 검색·경로·정적 지도 엔드포인트이다.

Google Maps Platform은 이 FastAPI 백엔드에서만 호출한다. Streamlit 화면은 장소
데이터·경로 요약·프록시 이미지 바이트만 받고, Google API 키나 키가 포함된 Static
Maps URL은 받지 않는다.
"""

import hashlib
import json
import logging
import time
from datetime import date, datetime, timedelta
from typing import Literal
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status

from app.cache import cache_get, cache_set
from app.db import get_user_client
from app.deps import CurrentUser, get_current_user
from app.google_maps_client import (
    Coordinates,
    GoogleMapsClient,
    GoogleMapsError,
    GoogleMapsUnavailableError,
    StaticMapMarker,
)
from app.routers.trips import _owned_day, _owned_trip, touch_trip
from app.schemas import GooglePlaceItineraryCreate


router = APIRouter(tags=["maps"])
LOGGER = logging.getLogger(__name__)
PLACE_SEARCH_CACHE_TTL_SECONDS = 300
PLACE_DETAILS_CACHE_TTL_SECONDS = 86_400
ROUTE_CACHE_TTL_SECONDS = 900
WEATHER_CACHE_TTL_SECONDS = 3_600
RouteTravelMode = Literal["walk", "transit", "drive", "bicycle"]
# 이 실습 프로젝트에서 Redis는 선택 사항이다. 작은 프로세스 내부 대체 캐시는
# Redis가 설정되지 않았을 때 지도 JSON 요청과 바로 이어지는 이미지 요청이 같은
# 유료 경로를 두 번 계산하지 않게 한다.
_ROUTE_MEMORY_CACHE: dict[str, tuple[float, dict]] = {}


def _cache_key(prefix: str, values: object) -> str:
    """긴 검색어를 직접 넣지 않고 길이가 제한된 Redis 키를 반환한다."""

    serialized = json.dumps(values, ensure_ascii=False, sort_keys=True, default=str)
    return f"{prefix}:{hashlib.sha256(serialized.encode('utf-8')).hexdigest()}"


def _maps_client() -> GoogleMapsClient:
    """설정된 Maps 클라이언트를 만들거나 UI에 안전한 설정 안내를 제공한다."""

    try:
        return GoogleMapsClient.from_environment()
    except GoogleMapsUnavailableError as error:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(error)) from error


def _maps_request_error(error: GoogleMapsError) -> HTTPException:
    """Google 요청 URL이나 키를 노출하지 않고 제공자 오류를 변환한다."""

    return HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(error))


def _search_response(maps: GoogleMapsClient, text_query: str, destination: str | None, max_results: int) -> dict:
    """여행별 검색어에 대한 캐시된 Places 텍스트 검색 카드를 반환한다."""

    # 여행지를 붙이면 "카페" 같은 단순 검색어도 올바른 도시에 집중하면서,
    # 정확한 장소 이름을 입력하는 경우도 허용할 수 있다.
    full_query = " ".join(part for part in (text_query.strip(), (destination or "").strip()) if part)
    cache_key = _cache_key("google_places_search", {"query": full_query, "limit": max_results})
    cached = cache_get(cache_key)
    if cached:
        try:
            return json.loads(cached)
        except json.JSONDecodeError:
            # 형식이 잘못된 선택적 캐시 항목이 실제 검색을 막으면 안 된다.
            pass

    try:
        places = maps.search_places(full_query, max_results=max_results, language_code="ko")
    except GoogleMapsError as error:
        raise _maps_request_error(error) from error

    response = {
        "query": text_query.strip(),
        "resolved_query": full_query,
        "places": [place.as_place_row() for place in places],
    }
    cache_set(cache_key, json.dumps(response, ensure_ascii=False), PLACE_SEARCH_CACHE_TTL_SECONDS)
    return response


def _google_place_row(maps: GoogleMapsClient, google_place_id: str) -> dict:
    """Google이 직접 검증한 캐시된 정규화 장소 행을 반환한다."""

    normalized_id = google_place_id.removeprefix("places/").strip()
    cache_key = _cache_key("google_place_details", normalized_id)
    cached = cache_get(cache_key)
    if cached:
        try:
            value = json.loads(cached)
            if isinstance(value, dict):
                # Redis에는 레거시 ``provider_place_id`` 호환 필드가 추가되기 전에
                # 기록한 장소 상세 행이 남아 있을 수 있다. 백엔드 업데이트 뒤에
                # 사용자에게 Redis 비우기를 요구하는 대신, 메모리에서 보정하고
                # 짧은 수명의 캐시를 새로 갱신한다.
                if not value.get("provider_place_id"):
                    value["provider_place_id"] = str(
                        value.get("google_place_id") or normalized_id
                    )
                    cache_set(
                        cache_key,
                        json.dumps(value, ensure_ascii=False),
                        PLACE_DETAILS_CACHE_TTL_SECONDS,
                    )
                return value
        except json.JSONDecodeError:
            pass

    try:
        row = maps.get_place_details(normalized_id, language_code="ko").as_place_row()
    except GoogleMapsError as error:
        raise _maps_request_error(error) from error
    cache_set(cache_key, json.dumps(row, ensure_ascii=False), PLACE_DETAILS_CACHE_TTL_SECONDS)
    return row


def _upsert_place(client, values: dict) -> dict:
    """Google 장소 하나를 공용 참조 테이블에 캐시하고 UUID 행을 반환한다."""

    # 레거시 필수 필드를 빠뜨린 오래된 Redis 캐시 항목이나 이후 제공자 매퍼에
    # 대비한다. 이 애플리케이션에서 두 식별자는 모두 Google의 검증된 장소 ID를
    # 가리킨다.
    values = dict(values)
    values["provider_place_id"] = values.get("provider_place_id") or values.get(
        "google_place_id"
    )
    try:
        result = client.table("places").upsert(values, on_conflict="google_place_id").execute()
    except Exception as error:
        # 일반적으로 일회성 places 마이그레이션이나 RLS 정책을 적용하지 않았다는
        # 뜻이다. Supabase 연결 상세를 노출하지 않는다. 브라우저 메시지는 일반적으로
        # 유지하되, 로컬 스키마나 RLS 문제를 사용자에게 데이터베이스 상세를 공개하지
        # 않고 진단할 수 있도록 Supabase의 안전한 오류 코드·메시지는 백엔드 터미널에
        # 남긴다.
        LOGGER.warning("Google place cache upsert failed: %s", error)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="장소 정보를 저장하지 못했습니다. Supabase places 설정을 확인하세요.",
        ) from error
    if result.data:
        return result.data[0]

    # 제한적인 RETURNING 정책은 성공한 upsert 결과도 비어 있게 만들 수 있다.
    # 사용자에게 설정 문제를 알리기 전에 일반 select로 한 번 더 확인한다.
    try:
        fetched = (
            client.table("places")
            .select("id")
            .eq("google_place_id", values["google_place_id"])
            .limit(1)
            .execute()
            .data
        )
    except Exception as error:
        LOGGER.warning("Google place cache readback failed (%s).", type(error).__name__)
        fetched = []
    if fetched:
        return fetched[0]
    raise HTTPException(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        detail="저장한 장소 정보를 찾지 못했습니다. Supabase places 설정을 확인하세요.",
    )


def _day_place_markers(client, trip_id: UUID | str, day_id: UUID | str) -> tuple[list[dict], int]:
    """하루 일정 순서대로 실제 Google 장소 마커를 반환한다.

    AI 및 직접 입력한 텍스트 항목은 의도적으로 여기서 지오코딩하지 않는다. 검증되지
    않은 제목은 현실의 잘못된 장소를 가리키고 불필요한 유료 조회를 일으킬 수 있다.
    사용자가 명시적으로 장소를 선택하기 전까지는 Google 검색에서 저장한 항목만 지도
    마커가 된다.
    """

    items = (
        client.table("itinerary_items")
        .select("id,title,place_id,start_at,sort_order")
        .eq("trip_id", str(trip_id))
        .eq("trip_day_id", str(day_id))
        .execute()
        .data
    )
    place_ids = [str(item["place_id"]) for item in items if item.get("place_id")]
    if not place_ids:
        return [], 0

    try:
        place_rows = (
            client.table("places")
            .select("id,display_name,formatted_address,latitude,longitude,google_place_id")
            .in_("id", place_ids)
            .execute()
            .data
        )
    except Exception as error:
        LOGGER.warning("Google place marker lookup failed (%s).", type(error).__name__)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="지도용 장소 정보를 불러오지 못했습니다. Supabase places 설정을 확인하세요.",
        ) from error

    places_by_id = {str(row["id"]): row for row in place_rows}
    ordered_items = sorted(
        items,
        key=lambda item: (
            item.get("start_at") is None,
            str(item.get("start_at") or ""),
            int(item.get("sort_order") or 0),
        ),
    )
    markers: list[dict] = []
    skipped_count = 0
    for item in ordered_items:
        place = places_by_id.get(str(item.get("place_id") or ""))
        if not place:
            continue
        try:
            coordinates = Coordinates(
                latitude=float(place["latitude"]), longitude=float(place["longitude"])
            )
        except (KeyError, TypeError, ValueError):
            skipped_count += 1
            continue
        markers.append(
            {
                "sequence": len(markers) + 1,
                "itinerary_item_id": item["id"],
                "place_id": place["id"],
                "google_place_id": place.get("google_place_id"),
                "title": item.get("title") or place.get("display_name") or "장소",
                "address": place.get("formatted_address"),
                "latitude": coordinates.latitude,
                "longitude": coordinates.longitude,
            }
        )
    return markers, skipped_count


def _route_for_markers(
    maps: GoogleMapsClient, markers: list[dict], travel_mode: RouteTravelMode
) -> dict | None:
    """정렬된 하루 전체에 Routes를 한 번 호출하고 간결한 응답을 캐시한다."""

    if len(markers) < 2:
        return None
    signature = {
        "mode": travel_mode,
        "points": [
            (marker["itinerary_item_id"], marker["latitude"], marker["longitude"])
            for marker in markers
        ],
    }
    cache_key = _cache_key("google_routes", signature)
    cached = cache_get(cache_key)
    if cached:
        try:
            value = json.loads(cached)
            if isinstance(value, dict):
                return value
        except json.JSONDecodeError:
            pass

    memory_entry = _ROUTE_MEMORY_CACHE.get(cache_key)
    if memory_entry and memory_entry[0] > time.monotonic():
        return memory_entry[1]

    points = tuple(
        Coordinates(latitude=marker["latitude"], longitude=marker["longitude"])
        for marker in markers
    )
    try:
        result = maps.compute_route(
            points[0],
            points[-1],
            travel_mode=travel_mode,
            intermediates=points[1:-1],
            language_code="ko",
        )
    except GoogleMapsError as error:
        raise _maps_request_error(error) from error
    route = {
        "travel_mode": travel_mode,
        "duration_seconds": result.duration_seconds,
        "distance_meters": result.distance_meters,
        "encoded_polyline": result.encoded_polyline,
    }
    cache_set(cache_key, json.dumps(route, ensure_ascii=False), ROUTE_CACHE_TTL_SECONDS)
    _ROUTE_MEMORY_CACHE[cache_key] = (time.monotonic() + ROUTE_CACHE_TTL_SECONDS, route)
    return route


def _automatic_route_plan(maps: GoogleMapsClient, markers: list[dict]) -> dict:
    """각 장소 사이에서 도보 20분 기준으로 현실적인 이동 수단을 선택한다."""

    legs: list[dict] = []
    route_segments: list[dict] = []
    total_distance = 0
    total_duration = 0.0
    unknown_count = 0
    for origin, destination in zip(markers, markers[1:]):
        pair = [origin, destination]
        walk = None
        try:
            walk = _route_for_markers(maps, pair, "walk")
        except HTTPException:
            pass

        selected = walk if walk and float(walk["duration_seconds"]) <= 20 * 60 else None
        if selected is None:
            alternatives = []
            for mode in ("transit", "drive"):
                try:
                    route = _route_for_markers(maps, pair, mode)
                except HTTPException:
                    continue
                if route:
                    alternatives.append(route)
            if alternatives:
                selected = min(alternatives, key=lambda route: float(route["duration_seconds"]))

        leg = {
            "from_itinerary_item_id": origin["itinerary_item_id"],
            "to_itinerary_item_id": destination["itinerary_item_id"],
            "from_title": origin["title"],
            "to_title": destination["title"],
            "status": "ok" if selected else "unknown",
        }
        if selected:
            leg.update(selected)
            total_distance += int(selected["distance_meters"])
            total_duration += float(selected["duration_seconds"])
            if selected.get("encoded_polyline"):
                route_segments.append(
                    {
                        "travel_mode": selected["travel_mode"],
                        "encoded_polyline": selected["encoded_polyline"],
                    }
                )
        else:
            leg["travel_mode"] = None
            leg["duration_seconds"] = None
            leg["distance_meters"] = None
            unknown_count += 1
        legs.append(leg)
    return {
        "legs": legs,
        "route_segments": route_segments,
        "total_distance_meters": total_distance,
        "total_duration_seconds": total_duration,
        "unknown_leg_count": unknown_count,
    }


def _weather_for_day(maps: GoogleMapsClient, trip: dict, day: dict, markers: list[dict]) -> dict:
    """예보 범위 안의 여행일만 조회하고 설정·범위 오류는 안내값으로 돌려준다."""

    if not markers:
        return {"status": "unavailable", "label": "장소 좌표 없음"}
    try:
        travel_date = date.fromisoformat(str(day["travel_date"]))
        local_today = datetime.now(ZoneInfo(str(trip.get("timezone") or "UTC"))).date()
    except (KeyError, TypeError, ValueError, ZoneInfoNotFoundError):
        return {"status": "unavailable", "label": "날짜 확인 필요"}
    offset = (travel_date - local_today).days
    if offset < 0:
        return {"status": "unavailable", "label": "지난 날짜"}
    if offset > 9:
        return {"status": "pending", "label": "예보 전"}

    signature = {"lat": markers[0]["latitude"], "lng": markers[0]["longitude"], "date": str(travel_date)}
    cache_key = _cache_key("google_weather", signature)
    cached = cache_get(cache_key)
    if cached:
        try:
            return json.loads(cached)
        except json.JSONDecodeError:
            pass
    try:
        forecasts = maps.get_daily_forecast(
            Coordinates(markers[0]["latitude"], markers[0]["longitude"]), days=offset + 1
        )
    except GoogleMapsError:
        return {"status": "unavailable", "label": "예보 확인 안 됨"}
    forecast = next(
        (
            value for value in forecasts
            if value.get("displayDate") == {
                "year": travel_date.year, "month": travel_date.month, "day": travel_date.day
            }
        ),
        None,
    )
    if not forecast:
        return {"status": "unavailable", "label": "예보 확인 안 됨"}
    daytime = forecast.get("daytimeForecast") or {}
    condition = daytime.get("weatherCondition") or {}
    result = {
        "status": "ok",
        "label": str((condition.get("description") or {}).get("text") or "날씨 정보"),
        "min_celsius": (forecast.get("minTemperature") or {}).get("degrees"),
        "max_celsius": (forecast.get("maxTemperature") or {}).get("degrees"),
        "precipitation_percent": (((daytime.get("precipitation") or {}).get("probability") or {}).get("percent")),
    }
    cache_set(cache_key, json.dumps(result, ensure_ascii=False), WEATHER_CACHE_TTL_SECONDS)
    return result


def _day_map_payload(
    client,
    trip_id: UUID | str,
    day_id: UUID | str,
    travel_mode: RouteTravelMode,
) -> tuple[dict, GoogleMapsClient]:
    """공개 마커·경로 데이터를 만들고 이미지 프록시에 쓸 Maps 클라이언트를 유지한다."""

    _owned_trip(client, trip_id)
    _owned_day(client, trip_id, day_id)
    markers, skipped_count = _day_place_markers(client, trip_id, day_id)
    if not markers:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="이 DAY에는 지도에 표시할 Google 검색 장소가 없습니다. 장소 검색에서 일정을 추가해 주세요.",
        )

    maps = _maps_client()
    route = None
    route_warning = None
    if len(markers) >= 2:
        try:
            route = _route_for_markers(maps, markers, travel_mode)
        except HTTPException as error:
            # Routes가 활성화되지 않았거나 경로를 찾지 못해도 마커는 유용하다.
            # 지도 옆에 안전한 제공자 메시지를 함께 반환한다.
            route_warning = str(error.detail)

    return (
        {
            "day_id": str(day_id),
            "markers": markers,
            "skipped_item_count": skipped_count,
            "route": route,
            "route_warning": route_warning,
        },
        maps,
    )


@router.get("/trips/{trip_id}/days/{day_id}/places/search")
def search_trip_places(
    trip_id: UUID,
    day_id: UUID,
    query: str = Query(min_length=1, max_length=500),
    max_results: int = Query(default=6, ge=1, le=10),
    current_user: CurrentUser = Depends(get_current_user),
):
    """키를 노출하지 않고 접근 가능한 여행의 여행지에서 Google Places를 검색한다."""

    client = get_user_client(current_user.token)
    trip = _owned_trip(client, trip_id)
    _owned_day(client, trip_id, day_id)
    return _search_response(_maps_client(), query, trip.get("destination"), max_results)


@router.post(
    "/trips/{trip_id}/days/{day_id}/google-places",
    status_code=status.HTTP_201_CREATED,
)
def add_google_place_to_day(
    trip_id: UUID,
    day_id: UUID,
    payload: GooglePlaceItineraryCreate,
    current_user: CurrentUser = Depends(get_current_user),
):
    """Google 장소를 검증·캐시하고 일정 항목으로 추가한다."""

    client = get_user_client(current_user.token)
    _owned_trip(client, trip_id)
    day = _owned_day(client, trip_id, day_id)
    try:
        travel_date = date.fromisoformat(str(day["travel_date"]))
    except (KeyError, TypeError, ValueError) as error:
        raise HTTPException(status_code=500, detail="DAY 날짜 정보가 올바르지 않습니다.") from error
    if payload.start_at.date() != travel_date:
        raise HTTPException(status_code=400, detail="선택한 DAY 날짜 안에서만 장소를 추가할 수 있습니다.")

    place = _upsert_place(client, _google_place_row(_maps_client(), payload.google_place_id))
    last_item = (
        client.table("itinerary_items")
        .select("sort_order")
        .eq("trip_id", str(trip_id))
        .order("sort_order", desc=True)
        .limit(1)
        .execute()
        .data
    )
    values = {
        "trip_id": str(trip_id),
        "trip_day_id": str(day_id),
        "place_id": place["id"],
        "item_type": payload.item_type,
        "source": "google_search",
        "title": place.get("display_name") or "Google 검색 장소",
        "start_at": payload.start_at.isoformat(),
        "end_at": (payload.start_at + timedelta(minutes=payload.estimated_stay_minutes)).isoformat(),
        "estimated_stay_minutes": payload.estimated_stay_minutes,
        "is_fixed": payload.is_fixed,
        "travel_mode": payload.travel_mode,
        "notes": payload.notes.strip() if payload.notes else None,
        "sort_order": (last_item[0]["sort_order"] + 1) if last_item else 0,
    }
    try:
        result = client.table("itinerary_items").insert(values).execute()
    except Exception as error:
        LOGGER.warning("Google place itinerary insert failed (%s).", type(error).__name__)
        raise HTTPException(status_code=500, detail="검색 장소를 일정에 추가하지 못했습니다.") from error
    if not result.data:
        raise HTTPException(status_code=400, detail="검색 장소를 일정에 추가하지 못했습니다.")
    touch_trip(client, trip_id)
    return result.data[0]


@router.get("/trips/{trip_id}/days/{day_id}/map")
def read_day_map(
    trip_id: UUID,
    day_id: UUID,
    travel_mode: RouteTravelMode = Query(default="walk"),
    current_user: CurrentUser = Depends(get_current_user),
):
    """DAY 하나의 정렬된 마커와 실제 Google Routes 요약을 반환한다."""

    client = get_user_client(current_user.token)
    payload, _ = _day_map_payload(client, trip_id, day_id, travel_mode)
    return payload


@router.get("/trips/{trip_id}/days/{day_id}/route-plan")
def read_day_route_plan(
    trip_id: UUID,
    day_id: UUID,
    current_user: CurrentUser = Depends(get_current_user),
):
    """DAY의 자동 이동 수단·구간·총합과 선택 날짜의 날씨를 반환한다."""

    client = get_user_client(current_user.token)
    trip = _owned_trip(client, trip_id)
    day = _owned_day(client, trip_id, day_id)
    markers, skipped_count = _day_place_markers(client, trip_id, day_id)
    if not markers:
        return {
            "day_id": str(day_id), "markers": [], "skipped_item_count": skipped_count,
            "legs": [], "route_segments": [], "total_distance_meters": 0,
            "total_duration_seconds": 0, "unknown_leg_count": 0,
            "weather": {"status": "unavailable", "label": "장소 좌표 없음"},
        }
    maps = _maps_client()
    plan = _automatic_route_plan(maps, markers)
    return {
        "day_id": str(day_id), "markers": markers, "skipped_item_count": skipped_count,
        **plan, "weather": _weather_for_day(maps, trip, day, markers),
    }


@router.get("/trips/{trip_id}/days/{day_id}/map/image")
def read_day_map_image(
    trip_id: UUID,
    day_id: UUID,
    travel_mode: RouteTravelMode = Query(default="walk"),
    current_user: CurrentUser = Depends(get_current_user),
):
    """마커와 Routes 폴리라인이 있는 Google Static Maps 이미지를 프록시한다."""

    client = get_user_client(current_user.token)
    payload, maps = _day_map_payload(client, trip_id, day_id, travel_mode)
    markers = tuple(
        StaticMapMarker(
            coordinates=Coordinates(latitude=marker["latitude"], longitude=marker["longitude"]),
            # Maps Static 라벨은 영문 또는 숫자 한 글자만 허용한다. 하루에 10곳
            # 이상이 있어도 화면의 텍스트에서는 모든 순서 번호를 계속 표시한다.
            label=str(marker["sequence"]) if marker["sequence"] <= 9 else None,
            color="0x3169E8",
        )
        for marker in payload["markers"]
    )
    try:
        image = maps.fetch_static_map_image(
            markers=markers,
            encoded_polyline=(payload.get("route") or {}).get("encoded_polyline"),
            width=640,
            height=380,
            scale=2,
        )
    except GoogleMapsError as error:
        raise _maps_request_error(error) from error
    return Response(
        content=image.content,
        media_type=image.content_type,
        headers={"Cache-Control": "private, max-age=120"},
    )
