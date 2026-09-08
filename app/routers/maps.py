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
from app.openmeteo_client import (
    OpenMeteoClient,
    OpenMeteoError,
)
from app.routers.trips import _owned_day, _owned_trip, touch_trip
from app.schemas import AccommodationPlaceUpdate, GooglePlaceItineraryCreate
from app.services.destination_scope import DestinationScope, resolve_destination_scope

#LSW 수정 0908
from app.services.destination_scope import (
    DestinationScope,
    #여행 만들기 화면이 도시 후보 목록을 받는다.
    # 화면에 보여 줄 도시 이름("도쿄도" 대신 "도쿄")을 서버가 정한다.
    display_label,
    list_destination_candidates,
    resolve_destination_scope,
)


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
_DESTINATION_SCOPE_MEMORY_CACHE: dict[str, tuple[float, DestinationScope]] = {}
DESTINATION_SCOPE_CACHE_TTL_SECONDS = 3_600

#LSW 수정 0908
# 검색어 한 글자마다 유료 Places 호출이 나가지 않도록 도시 범위 캐시와 같은 수명(1시간)을 준다.
DESTINATION_SEARCH_CACHE_TTL_SECONDS = 3_600

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


def _destination_scope_for_search(
    maps: GoogleMapsClient, destination: str | None
) -> DestinationScope | None:
    """여행 도시 범위를 짧게 재사용해 다른 나라·도시 검색 결과를 막는다."""

    normalized_destination = " ".join(str(destination or "").casefold().split())
    if not normalized_destination:
        return None
    cached = _DESTINATION_SCOPE_MEMORY_CACHE.get(normalized_destination)
    if cached and cached[0] > time.monotonic():
        return cached[1]
    try:
        scope = resolve_destination_scope(maps, str(destination))
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    except GoogleMapsError as error:
        raise _maps_request_error(error) from error
    _DESTINATION_SCOPE_MEMORY_CACHE[normalized_destination] = (
        time.monotonic() + DESTINATION_SCOPE_CACHE_TTL_SECONDS,
        scope,
    )
    return scope

#LSW 수정 0908
def _country_name(place) -> str | None:
    """도시 응답에서 나라 이름만 꺼낸다.

    [변경 사유] DestinationScope.country_names 는 비교용이라 casefold 된
    frozenset 이다. 화면에 그대로 쓸 수 없다. formatted_address 에서 나라를
    잘라내는 방법도 쓰지 않는다 — 표기 순서가 언어마다 달라 추측이 된다.
    Google 이 country 로 표시한 구성요소만 읽는다.
    """
    for component in place.address_components:
        if "country" in component.types:
            return component.long_text or component.short_text or None
    return None


def _places_inside_destination(
    maps: GoogleMapsClient, scope: DestinationScope | None, candidates: list
) -> list:
    """Google 응답도 도시·국가·좌표 기준으로 다시 확인해 범위 밖 후보를 제거한다."""

    if scope is None:
        return candidates
    accepted = [place for place in candidates if scope.accepts(place)]
    if accepted:
        return accepted
    # 도시명 표기가 한국어·영어로 달라지는 경우에만 같은 Google 도시 ID의 영문
    # 주소 별칭을 한 번 보완한다. 이 과정에서도 범위를 넓히지는 않는다.
    if not any(
        scope.rejection_reason(place) in {"city_name_mismatch", "administrative_name_mismatch"}
        for place in candidates
    ):
        return []
    try:
        scope_with_aliases = scope.with_city_aliases(
            maps.get_city_details(scope.google_place_id, language_code="en")
        )
    except (GoogleMapsError, ValueError):
        return []
    return [place for place in candidates if scope_with_aliases.accepts(place)]


def _day_reference_location(client, trip_id: UUID | str, day_id: UUID | str) -> Coordinates | None:
    """현재 DAY에서 좌표가 확인된 첫 일정 장소를 검색 우선 위치로 사용한다."""

    try:
        markers, _ = _day_place_markers(client, trip_id, day_id)
    except HTTPException:
        # 기준 좌표가 없어도 도시 범위 제한 검색은 가능하므로, 검색 자체를 막지
        # 않고 아래의 destination scope 검증으로 안전성을 유지한다.
        return None
    if not markers:
        return None
    marker = markers[0]
    return Coordinates(latitude=float(marker["latitude"]), longitude=float(marker["longitude"]))


def _search_response(
    maps: GoogleMapsClient,
    text_query: str,
    destination: str | None,
    max_results: int,
    *,
    location_bias: Coordinates | None = None,
) -> dict:
    """여행별 검색어에 대한 캐시된 Places 텍스트 검색 카드를 반환한다."""

    # 여행지를 붙이면 "카페" 같은 단순 검색어도 올바른 도시에 집중하면서,
    # 정확한 장소 이름을 입력하는 경우도 허용할 수 있다.
    full_query = " ".join(part for part in (text_query.strip(), (destination or "").strip()) if part)
    cache_key = _cache_key(
        "google_places_search",
        {
            "query": full_query,
            "limit": max_results,
            # 이전에는 도시 범위 재검증 전 응답이 캐시될 수 있었다. 같은 검색어라도
            # 새 필터 정책의 결과를 즉시 쓰도록 캐시 서명을 분리한다.
            "scope_filter": "destination-v1",
            # 기준 장소 주변 추천은 일반 도시 검색과 캐시를 분리해야 한다.
            "location_bias": (
                (round(location_bias.latitude, 5), round(location_bias.longitude, 5))
                if location_bias
                else None
            ),
        },
    )
    cached = cache_get(cache_key)
    if cached:
        try:
            return json.loads(cached)
        except json.JSONDecodeError:
            # 형식이 잘못된 선택적 캐시 항목이 실제 검색을 막으면 안 된다.
            pass

    scope = _destination_scope_for_search(maps, destination)
    try:
        places = maps.search_places(
            full_query,
            max_results=max_results,
            language_code="ko",
            location_bias=location_bias,
            radius_meters=2_500,
            # 지도 기준점이 없을 때는 Google에도 도시 사각 범위를 엄격히 전달한다.
            # 기준점이 있으면 bias와 restriction을 함께 보낼 수 없으므로, 아래의
            # scope.accepts 검사가 도시·국가 밖 결과를 최종적으로 제거한다.
            location_restriction=scope.viewport if scope and location_bias is None else None,
            include_region_metadata=scope is not None,
        )
    except GoogleMapsError as error:
        raise _maps_request_error(error) from error

    scoped_places = _places_inside_destination(maps, scope, places)
    response = {
        "query": text_query.strip(),
        "resolved_query": full_query,
        "places": [place.as_place_row() for place in scoped_places],
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


def _weather_for_day(trip: dict, day: dict, markers: list[dict]) -> dict:
    """Open-Meteo 예보를 여행 화면에서 쓸 수 있는 일별 값으로 반환한다."""

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
    # Open-Meteo에 오늘을 포함한 최대 16일을 요청하므로 offset 0~15까지만
    # 조회한다. 그 이후 날짜는 정확하지 않은 예상 날씨 대신 안내만 표시한다.
    if offset > 15:
        return {"status": "pending", "label": "예보 전"}

    signature = {"lat": markers[0]["latitude"], "lng": markers[0]["longitude"], "date": str(travel_date)}
    cache_key = _cache_key("openmeteo_daily", signature)
    cached = cache_get(cache_key)
    if cached:
        try:
            return json.loads(cached)
        except json.JSONDecodeError:
            pass
    try:
        forecasts = OpenMeteoClient().get_daily_forecasts(
            Coordinates(markers[0]["latitude"], markers[0]["longitude"])
        )
    except OpenMeteoError:
        return {"status": "unavailable", "label": "예보 확인 안 됨"}
    forecast = next(
        (value for value in forecasts if value.get("date") == str(travel_date)),
        None,
    )
    if not forecast:
        return {"status": "pending", "label": "예보 전"}
    result = {
        "status": "ok",
        "label": str(forecast.get("label") or "날씨 정보"),
        "min_celsius": forecast.get("min_celsius"),
        "max_celsius": forecast.get("max_celsius"),
        "precipitation_percent": forecast.get("precipitation_percent"),
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


@router.get("/trips/{trip_id}/accommodation/places/search")
def search_trip_accommodation_places(
    trip_id: UUID,
    query: str = Query(min_length=1, max_length=500),
    max_results: int = Query(default=3, ge=1, le=6),
    current_user: CurrentUser = Depends(get_current_user),
):
    """여행 도시 안에서 숙소 후보를 검색한다.

    DAY의 첫 장소를 기준점으로 삼는 일반 일정 검색과 달리, 숙소는 아직 일정에
    없을 수 있으므로 여행 도시 범위만 엄격히 적용한다.
    """

    client = get_user_client(current_user.token)
    trip = _owned_trip(client, trip_id)
    return _search_response(
        _maps_client(),
        query,
        trip.get("destination"),
        max_results,
    )


@router.post("/trips/{trip_id}/accommodation")
def set_trip_accommodation(
    trip_id: UUID,
    payload: AccommodationPlaceUpdate,
    current_user: CurrentUser = Depends(get_current_user),
):
    """Google Place ID로 확인한 실제 장소 하나를 여행 숙소로 저장한다."""

    client = get_user_client(current_user.token)
    _owned_trip(client, trip_id)
    place = _upsert_place(client, _google_place_row(_maps_client(), payload.google_place_id))
    try:
        result = (
            client.table("trips")
            .update({"accommodation_place_id": str(place["id"])})
            .eq("id", str(trip_id))
            .execute()
        )
    except Exception as error:
        LOGGER.warning("여행 숙소 저장 실패 (%s).", type(error).__name__)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="숙소를 저장하지 못했습니다. Supabase 숙소 설정을 확인하세요.",
        ) from error
    if not result.data:
        raise HTTPException(status_code=400, detail="숙소를 저장하지 못했습니다.")
    touch_trip(client, trip_id)
    return {"trip": result.data[0], "place": place}

#LSW 수정 0908
@router.get("/destinations/search", dependencies=[Depends(get_current_user)])
def search_destinations(query: str = Query(min_length=2, max_length=100)):
    """여행을 만들기 전에 Google이 도시로 확인한 후보만 보여준다.
     여행 생성은 도시 범위를 하나로 좁히지 못하면 거절하는데 n그 판정이 Gemini 호출(최대 180초) 뒤에 일어난다. 여기서 먼저 고르게 하면
    사용자가 생성 시간을 다 기다린 뒤에 422 를 받는 일이 없고 버려지는
    LLM 호출도 없다.

    여행 소유권을 확인할 여행이 아직 없으므로 로그인만 요구한다.
    trip_id 를 요구하는 기존 검색과 달리 여행 만들기 화면에서 부를 수 있어야 한다.
    """
    cleaned = query.strip()
    cache_key = _cache_key("destination_search", {"query": cleaned.casefold(), "v": 1})
    cached = cache_get(cache_key)
    if cached:
        try:
            return json.loads(cached)
        except json.JSONDecodeError:
            # 형식이 잘못된 선택적 캐시 항목이 실제 검색을 막으면 안 된다.
            pass

    try:
        candidates = list_destination_candidates(_maps_client(), cleaned)
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    except GoogleMapsError as error:
        raise _maps_request_error(error) from error

    destinations = []
    for place, _ in candidates:
        country = _country_name(place)
        destinations.append({
            "google_place_id": place.google_place_id,
            "display_name": place.display_name,
            "country": country,
            "formatted_address": place.formatted_address,
            "latitude": place.coordinates.latitude if place.coordinates else None,
            "longitude": place.coordinates.longitude if place.coordinates else None,
            # 여행 생성에 그대로 넣을 문자열을 서버가 만든다.
            # 화면이 이름과 나라를 다시 조합하면 표기가 갈리고, 생성 단계의
            # 도시 조회가 후보를 하나로 좁히지 못해 422 가 날 수 있다.
            "destination": f"{place.display_name}, {country}" if country else place.display_name,
            # 화면에 보여 줄 이름. Google 표기가 "도쿄도"라 그대로 쓰면 어색하다.
            # 위 destination 과 일부러 나눠 둔다 — 보내는 값은 Google 표기여야
            # 생성 단계에서 도시를 다시 찾을 수 있고, 읽는 값은 사람이 쓰는
            # 이름이어야 고르기 쉽다.
            "label": display_label(place),
        })

    response = {"query": cleaned, "destinations": destinations}
    # [변경 사유] 빈 결과는 담지 않는다. 담아 두면 두 가지가 한 시간 동안 고정된다 —
    # 서버를 고쳐 이제 찾을 수 있게 된 도시가 계속 "없음"으로 나오고(실제로
    # 도쿄·서울 허용 목록을 넣은 뒤에도 옛 빈 답이 그대로 나갔다), Google 이
    # 일시적으로 실패해 비어 온 답까지 굳어 버린다.
    # 오타는 사용자가 곧바로 고쳐 다시 치므로 같은 빈 검색이 반복될 일이 적다.
    if destinations:
        cache_set(
            cache_key,
            json.dumps(response, ensure_ascii=False),
            DESTINATION_SEARCH_CACHE_TTL_SECONDS,
        )
    return response
#LSW 수정 0908
@router.get("/destinations/places/search", dependencies=[Depends(get_current_user)])
def search_destination_places(
    destination: str = Query(min_length=1, max_length=100),
    query: str = Query(min_length=1, max_length=500),
    max_results: int = Query(default=5, ge=1, le=10),
):
    """여행을 만들기 전에도 고른 여행지 안에서만 장소를 찾는다.
    여행 안 검색(/trips/{trip_id}/...)과 같은 _search_response 를 쓴다.
    화면이 필요한 값과 도시 밖 결과를 거르는 기준이 같으므로, 검색 경로를 둘로
    나누면 한쪽만 고치는 어긋남이 생긴다. 다른 점은 소유권을 확인할 여행이
    아직 없다는 것뿐이라 도시 범위 검증만 그대로 적용한다.
    destination 을 필수로 받는다. 지역 없이 "스타벅스"를 찾으면
    전 세계 결과가 나오고 그중 무엇을 담아도 이 여행의 일정에 쓸 수 없다.
    화면이 잠금을 빠뜨려도 여기서 422 가 나서 전 세계 검색으로 새지 않는다.
    """
    return _search_response(_maps_client(), query, destination, max_results)



@router.get("/trips/{trip_id}/days/{day_id}/places/search")
def search_trip_places(
    trip_id: UUID,
    day_id: UUID,
    query: str = Query(min_length=1, max_length=500),
    max_results: int = Query(default=6, ge=1, le=10),
    near_latitude: float | None = Query(default=None, ge=-90, le=90),
    near_longitude: float | None = Query(default=None, ge=-180, le=180),
    current_user: CurrentUser = Depends(get_current_user),
):
    """키를 노출하지 않고 접근 가능한 여행의 여행지에서 Google Places를 검색한다."""

    client = get_user_client(current_user.token)
    trip = _owned_trip(client, trip_id)
    _owned_day(client, trip_id, day_id)
    if (near_latitude is None) != (near_longitude is None):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="주변 장소 검색에는 위도와 경도를 함께 입력하세요.",
        )
    location_bias = (
        Coordinates(latitude=near_latitude, longitude=near_longitude)
        if near_latitude is not None and near_longitude is not None
        # 추천 문장이 특정 장소를 가리킨 경우 프론트가 그 장소 좌표를 보낸다.
        # 일반 검색도 현재 DAY 안의 실제 장소 하나를 기준으로 우선 정렬한다.
        else _day_reference_location(client, trip_id, day_id)
    )
    return _search_response(
        _maps_client(),
        query,
        trip.get("destination"),
        max_results,
        location_bias=location_bias,
    )


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
        "source": payload.source,
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
        **plan, "weather": _weather_for_day(trip, day, markers),
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
