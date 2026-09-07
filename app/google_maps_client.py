"""백엔드 내부에서만 처리하는 Google Maps Platform 요청 모음이다.

이 모듈에는 의도적으로 FastAPI 라우터를 두지 않는다. 이후 보호된 라우터에서
:class:`GoogleMapsClient`를 만들고, 현재 사용자의 여행 소유권을 확인한 뒤,
화면에 필요한 장소·경로·이미지 데이터만 반환할 수 있다. Google Maps API 키는
Streamlit 프론트엔드에 넣거나 지도 URL 형태로 브라우저에 다시 보낼 필요가 없다.
"""

import json
import os
from dataclasses import dataclass
from typing import Any, Literal
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen


PLACES_TEXT_SEARCH_URL = "https://places.googleapis.com/v1/places:searchText"
PLACES_DETAILS_URL = "https://places.googleapis.com/v1/places"
ROUTES_COMPUTE_ROUTES_URL = "https://routes.googleapis.com/directions/v2:computeRoutes"
STATIC_MAP_URL = "https://maps.googleapis.com/maps/api/staticmap"

# 검색 카드를 그리고 나중에 최소한의 `places` 행을 만들 때 필요한 필드만 요청한다.
# 더 많은 필드를 요청하면 Places API 과금 단계가 높아질 수 있다.
PLACE_SEARCH_FIELD_MASK = ",".join(
    (
        "places.id",
        "places.displayName",
        "places.formattedAddress",
        "places.location",
        "places.rating",
        "places.userRatingCount",
        "places.primaryType",
        "places.types",
        "places.googleMapsUri",
    )
)
PLACE_DETAILS_FIELD_MASK = ",".join(
    (
        "id",
        "displayName",
        "formattedAddress",
        "location",
        "rating",
        "userRatingCount",
        "primaryType",
        "types",
        "googleMapsUri",
    )
)
# 도시 조회에서는 평점·리뷰를 요청하지 않는다. 검색 사각형과 구조화된 행정구역을
# 함께 사용하며, viewport 자체를 정확한 도시 경계 다각형이라고 간주하지 않는다.
CITY_SEARCH_FIELD_MASK = ",".join(
    f"places.{field}" for field in (
        "id", "displayName", "formattedAddress", "location", "types",
        "addressComponents", "viewport",
    )
)
ROUTE_FIELD_MASK = "routes.duration,routes.distanceMeters,routes.polyline.encodedPolyline"

RouteTravelMode = Literal["walk", "transit", "drive", "bicycle"]
_GOOGLE_ROUTE_TRAVEL_MODES: dict[RouteTravelMode, str] = {
    "walk": "WALK",
    "transit": "TRANSIT",
    "drive": "DRIVE",
    "bicycle": "BICYCLE",
}


class GoogleMapsError(RuntimeError):
    """완료할 수 없는 Maps Platform 요청의 기본 오류이다."""


class GoogleMapsUnavailableError(GoogleMapsError):
    """이 백엔드에서 Google Maps 설정을 의도적으로 비워 둔 경우 발생한다."""


class GoogleMapsRequestError(GoogleMapsError):
    """Google Maps가 요청을 거부하거나 연결할 수 없을 때 발생한다."""


@dataclass(frozen=True)
class Coordinates:
    """장소·경로·지도 호출에서 함께 쓰는 검증된 위도·경도 쌍이다."""

    latitude: float
    longitude: float

    def __post_init__(self) -> None:
        """유료 API를 호출하기 전에 불가능한 지리 좌표를 거부한다."""

        if not -90 <= self.latitude <= 90:
            raise ValueError("위도는 -90에서 90 사이여야 합니다.")
        if not -180 <= self.longitude <= 180:
            raise ValueError("경도는 -180에서 180 사이여야 합니다.")

    def as_google_lat_lng(self) -> dict[str, float]:
        """Places와 Routes API 요청이 기대하는 JSON 형태를 반환한다."""

        return {"latitude": self.latitude, "longitude": self.longitude}

    def as_static_map_value(self) -> str:
        """Maps Static API URL이 기대하는 간결한 좌표값을 반환한다."""

        return f"{self.latitude},{self.longitude}"


@dataclass(frozen=True)
class GeoViewport:
    """Google 검색에 쓸 사각 범위이다. 행정구역 경계 다각형은 아니다."""

    low: Coordinates
    high: Coordinates

    def __post_init__(self) -> None:
        """빈 위도 범위와 날짜변경선의 빈 경도 범위를 거절한다."""
        if self.low.latitude > self.high.latitude:
            raise ValueError("검색 범위의 남쪽은 북쪽보다 높을 수 없습니다.")
        if self.low.longitude == 180 and self.high.longitude == -180:
            raise ValueError("검색 범위의 경도가 비어 있습니다.")

    def contains(self, coordinates: Coordinates) -> bool:
        """경계점과 날짜변경선을 가로지르는 검색 사각형을 함께 처리한다."""
        if not self.low.latitude <= coordinates.latitude <= self.high.latitude:
            return False
        west, east = self.low.longitude, self.high.longitude
        if west > east:
            return coordinates.longitude >= west or coordinates.longitude <= east
        return west <= coordinates.longitude <= east

    def as_google_rectangle(self) -> dict[str, dict[str, float]]:
        """Text Search의 locationRestriction.rectangle 요청 값이다."""
        return {"low": self.low.as_google_lat_lng(), "high": self.high.as_google_lat_lng()}


@dataclass(frozen=True)
class AddressComponent:
    """주소 문자열의 부분 일치 대신 비교할 Google의 행정구역 구성요소이다."""

    long_text: str
    short_text: str
    types: tuple[str, ...]


@dataclass(frozen=True)
class PlaceResult:
    """데이터베이스에 저장하기 알맞게 추린 Places API(New) 장소 응답 일부이다."""

    google_place_id: str
    display_name: str
    formatted_address: str | None
    coordinates: Coordinates | None
    google_rating: float | None
    google_rating_count: int | None
    primary_type: str | None
    types: tuple[str, ...]
    google_maps_uri: str | None
    address_components: tuple[AddressComponent, ...] = ()
    viewport: GeoViewport | None = None

    @classmethod
    def from_google_payload(cls, payload: dict[str, Any]) -> "PlaceResult":
        """데이터베이스 스키마에 묶지 않고 Google 장소 객체 하나를 정규화한다."""

        place_id = str(payload.get("id") or "").strip()
        display_name_value = payload.get("displayName") or {}
        display_name = str(display_name_value.get("text") or "").strip()
        if not place_id or not display_name:
            raise GoogleMapsRequestError("Google Places 응답에 장소 ID 또는 이름이 없습니다.")

        location_value = payload.get("location")
        coordinates: Coordinates | None = None
        if location_value is not None:
            try:
                coordinates = Coordinates(
                    latitude=float(location_value["latitude"]),
                    longitude=float(location_value["longitude"]),
                )
            except (KeyError, TypeError, ValueError) as error:
                raise GoogleMapsRequestError(
                    "Google Places 응답의 좌표 형식이 올바르지 않습니다."
                ) from error

        rating = payload.get("rating")
        rating_count = payload.get("userRatingCount")
        address_components = []
        raw_components = payload.get("addressComponents") or []
        if not isinstance(raw_components, list):
            raise GoogleMapsRequestError("Google 장소의 행정구역 형식이 올바르지 않습니다.")
        for component in raw_components:
            if not isinstance(component, dict) or not isinstance(component.get("types", []), list):
                raise GoogleMapsRequestError("Google 장소의 행정구역 형식이 올바르지 않습니다.")
            address_components.append(AddressComponent(
                long_text=str(component.get("longText") or "").strip(),
                short_text=str(component.get("shortText") or "").strip(),
                types=tuple(str(value) for value in component.get("types", [])),
            ))
        viewport = None
        if payload.get("viewport") is not None:
            try:
                raw_viewport = payload["viewport"]
                viewport = GeoViewport(
                    low=Coordinates(float(raw_viewport["low"]["latitude"]), float(raw_viewport["low"]["longitude"])),
                    high=Coordinates(float(raw_viewport["high"]["latitude"]), float(raw_viewport["high"]["longitude"])),
                )
            except (KeyError, TypeError, ValueError) as error:
                raise GoogleMapsRequestError("Google 장소의 검색 범위 형식이 올바르지 않습니다.") from error
        try:
            return cls(
                google_place_id=place_id,
                display_name=display_name,
                formatted_address=_optional_text(payload.get("formattedAddress")),
                coordinates=coordinates,
                google_rating=float(rating) if rating is not None else None,
                google_rating_count=int(rating_count) if rating_count is not None else None,
                primary_type=_optional_text(payload.get("primaryType")),
                types=tuple(str(item) for item in payload.get("types") or ()),
                google_maps_uri=_optional_text(payload.get("googleMapsUri")),
                address_components=tuple(address_components),
                viewport=viewport,
            )
        except (TypeError, ValueError) as error:
            raise GoogleMapsRequestError(
                "Google Places 응답의 평점 형식이 올바르지 않습니다."
            ) from error

    def as_place_row(self) -> dict[str, Any]:
        """이후 ``places`` 삽입문에 바로 매핑할 수 있는 정규화된 필드를 반환한다.

        이 메서드는 일반 데이터만 반환하며 Supabase에 직접 저장하지 않는다.
        매핑을 이곳에 두면 Google 전용 중첩 JSON이 라우터나 최종 데이터베이스
        테이블로 새어 들어가는 일을 막을 수 있다.
        """

        return {
            # 기존 TripMate 스키마에는 이미 이 필수 레거시 식별자가 있다.
            # Google 장소 ID는 제공자 전용 식별자이므로, 검증된 같은 값을 두 표현에
            # 모두 쓰는 것이 올바르다.
            "provider_place_id": self.google_place_id,
            "google_place_id": self.google_place_id,
            "display_name": self.display_name,
            "formatted_address": self.formatted_address,
            "latitude": self.coordinates.latitude if self.coordinates else None,
            "longitude": self.coordinates.longitude if self.coordinates else None,
            "google_rating": self.google_rating,
            "google_rating_count": self.google_rating_count,
            "primary_type": self.primary_type,
            "types": list(self.types),
            "google_maps_uri": self.google_maps_uri,
        }


@dataclass(frozen=True)
class RouteResult:
    """TripMate에 필요한 경로 소요 시간·거리·인코딩 경로이다."""

    duration_seconds: float
    distance_meters: int
    encoded_polyline: str | None


@dataclass(frozen=True)
class StaticMapMarker:
    """Maps Static API 이미지에 그릴 수 있는 좌표 마커 하나이다."""

    coordinates: Coordinates
    label: str | None = None
    color: str | None = None

    def as_static_map_value(self) -> str:
        """라벨을 안전한 한 글자로 제한하면서 인코딩된 마커값을 만든다."""

        parts: list[str] = []
        if self.color:
            parts.append(f"color:{self.color}")
        if self.label:
            if len(self.label) != 1 or not self.label.isalnum():
                raise ValueError("지도 마커 라벨은 영문 또는 숫자 한 글자여야 합니다.")
            parts.append(f"label:{self.label.upper()}")
        parts.append(self.coordinates.as_static_map_value())
        return "|".join(parts)


@dataclass(frozen=True)
class StaticMapImage:
    """이후 FastAPI 프록시 엔드포인트가 바로 반환할 수 있는 이미지 바이트이다."""

    content: bytes
    content_type: str


def _optional_text(value: object) -> str | None:
    """선택적 API 문자열이 비어 있거나 없으면 ``None``으로 변환한다."""

    text = str(value or "").strip()
    return text or None


def _duration_seconds(value: object) -> float:
    """``\"123.4s\"`` 같은 Routes API protobuf 소요 시간 문자열을 해석한다."""

    text = str(value or "").strip()
    if not text.endswith("s"):
        raise GoogleMapsRequestError("Google Routes 응답의 소요 시간 형식이 올바르지 않습니다.")
    try:
        return float(text[:-1])
    except ValueError as error:
        raise GoogleMapsRequestError(
            "Google Routes 응답의 소요 시간 형식이 올바르지 않습니다."
        ) from error


class GoogleMapsClient:
    """Places·Routes·Static Maps API용 작은 백엔드 전용 HTTP 클라이언트이다."""

    def __init__(self, api_key: str, *, timeout_seconds: float = 10.0) -> None:
        """서버 측 키와 제한된 네트워크 대기 시간으로 클라이언트를 만든다."""

        cleaned_key = api_key.strip()
        if not cleaned_key:
            raise GoogleMapsUnavailableError(
                "Google 지도 기능이 설정되지 않았습니다. "
                "backend/.env의 GOOGLE_MAPS_API_KEY를 입력하세요."
            )
        if timeout_seconds <= 0:
            raise ValueError("Google Maps 요청 제한 시간은 0보다 커야 합니다.")
        self._api_key = cleaned_key
        self._timeout_seconds = timeout_seconds

    @classmethod
    def from_environment(cls) -> "GoogleMapsClient":
        """다른 계층에 노출하지 않고 백엔드 전용 API 키를 읽는다."""

        return cls(os.getenv("GOOGLE_MAPS_API_KEY", ""))

    def search_places(
        self,
        text_query: str,
        *,
        location_bias: Coordinates | None = None,
        radius_meters: float = 5_000,
        max_results: int = 10,
        language_code: str = "ko",
        location_restriction: GeoViewport | None = None,
        include_region_metadata: bool = False,
    ) -> list[PlaceResult]:
        """Places API(New) 텍스트 검색으로 장소 카드를 찾는다.

        ``location_bias``는 가까운 지역을 우선할 뿐, 다른 지역의 일치 장소를
        엄격히 제외하지는 않는다. 필드 마스크는 첫 TripMate 화면에 아직 필요하지
        않은 리뷰와 기타 데이터를 의도적으로 제외한다.
        """

        cleaned_query = text_query.strip()
        if not cleaned_query:
            raise ValueError("장소 검색어를 입력하세요.")
        if len(cleaned_query) > 500:
            raise ValueError("장소 검색어는 500자 이하여야 합니다.")
        if not 1 <= max_results <= 20:
            raise ValueError("검색 결과 수는 1에서 20 사이여야 합니다.")
        if location_bias is not None and not 1 <= radius_meters <= 50_000:
            raise ValueError("검색 반경은 1에서 50000m 사이여야 합니다.")
        if location_bias is not None and location_restriction is not None:
            raise ValueError("검색 우선 위치와 검색 제한 범위는 동시에 지정할 수 없습니다.")

        payload: dict[str, Any] = {
            "textQuery": cleaned_query,
            "pageSize": max_results,
            "languageCode": language_code,
        }
        if location_bias is not None:
            payload["locationBias"] = {
                "circle": {
                    "center": location_bias.as_google_lat_lng(),
                    "radius": radius_meters,
                }
            }
        if location_restriction is not None:
            payload["locationRestriction"] = {"rectangle": location_restriction.as_google_rectangle()}

        field_mask = PLACE_SEARCH_FIELD_MASK
        if include_region_metadata:
            field_mask += ",places.addressComponents"

        response = self._request_json(
            PLACES_TEXT_SEARCH_URL,
            method="POST",
            payload=payload,
            field_mask=field_mask,
        )
        places = response.get("places") or []
        if not isinstance(places, list):
            raise GoogleMapsRequestError("Google Places 응답의 장소 목록 형식이 올바르지 않습니다.")
        if not all(isinstance(place, dict) for place in places):
            raise GoogleMapsRequestError("Google Places 응답의 장소 목록 형식이 올바르지 않습니다.")
        return [PlaceResult.from_google_payload(place) for place in places]

    def search_city(self, destination: str, *, language_code: str = "ko") -> list[PlaceResult]:
        """여행당 한 번 도시 자체를 조회한다. 숙소·새 API 키·Geocoding API는 필요 없다.

        지정 유형 필터는 지리 검색에 적용되지 않을 수 있으므로 실제 응답의 유형을
        도시 검증 서비스에서 확인한다. 결과를 넓은 도/국가로 자동 대체하지 않는다.
        """
        query = destination.strip()
        if not query or len(query) > 100:
            raise ValueError("여행할 도시를 1~100자로 입력하세요.")
        response = self._request_json(
            PLACES_TEXT_SEARCH_URL,
            method="POST",
            payload={"textQuery": query, "pageSize": 5, "languageCode": language_code},
            field_mask=CITY_SEARCH_FIELD_MASK,
        )
        places = response.get("places") or []
        if not isinstance(places, list) or not all(isinstance(place, dict) for place in places):
            raise GoogleMapsRequestError("Google 도시 검색 결과의 형식이 올바르지 않습니다.")
        return [PlaceResult.from_google_payload(place) for place in places]

    def get_place_details(
        self,
        google_place_id: str,
        *,
        language_code: str = "ko",
    ) -> PlaceResult:
        """저장된 Google 장소 ID로 같은 간결한 장소 필드를 다시 조회한다."""

        place_id = google_place_id.removeprefix("places/").strip()
        if not place_id or "/" in place_id:
            raise ValueError("유효한 Google 장소 ID를 입력하세요.")
        query = urlencode({"languageCode": language_code})
        response = self._request_json(
            f"{PLACES_DETAILS_URL}/{quote(place_id, safe='')}?{query}",
            method="GET",
            field_mask=PLACE_DETAILS_FIELD_MASK,
        )
        return PlaceResult.from_google_payload(response)

    def get_city_details(self, google_place_id: str, *, language_code: str = "en") -> PlaceResult:
        """동일 도시 ID의 다른 언어 주소를 확인한다. 평점·리뷰는 요청하지 않는다.

        도시 검색과 장소 검색에 같은 languageCode를 써도 주소 구성요소의 언어가
        다를 수 있다. 번역 문자열을 추측하지 않고 동일 ID의 Google 응답을 사용한다.
        """
        place_id = google_place_id.removeprefix("places/").strip()
        if not place_id or "/" in place_id:
            raise ValueError("유효한 Google 도시 ID를 입력하세요.")
        response = self._request_json(
            f"{PLACES_DETAILS_URL}/{quote(place_id, safe='')}?{urlencode({'languageCode': language_code})}",
            method="GET",
            field_mask=CITY_SEARCH_FIELD_MASK.replace("places.", ""),
        )
        return PlaceResult.from_google_payload(response)

    def compute_route(
        self,
        origin: Coordinates,
        destination: Coordinates,
        *,
        travel_mode: RouteTravelMode = "transit",
        intermediates: tuple[Coordinates, ...] = (),
        traffic_aware: bool = False,
        language_code: str = "ko",
    ) -> RouteResult:
        """경로 하나를 계산하고 소요 시간·거리·폴리라인만 반환한다.

        기존 TripMate 항목값 ``flight``는 의도적으로 지원하지 않는다. Google
        Routes API는 항공 여정을 계산하지 않기 때문이다. 자동차 요청에는 교통량
        반영 시간을 선택할 수 있으며, Google이 자동차 경로에서만 이를 지원하므로
        다른 이동 수단에는 해당 필드를 넣지 않는다.
        """

        if travel_mode not in _GOOGLE_ROUTE_TRAVEL_MODES:
            raise ValueError("지원하지 않는 이동 수단입니다.")
        payload: dict[str, Any] = {
            "origin": {"location": {"latLng": origin.as_google_lat_lng()}},
            "destination": {"location": {"latLng": destination.as_google_lat_lng()}},
            "travelMode": _GOOGLE_ROUTE_TRAVEL_MODES[travel_mode],
            "computeAlternativeRoutes": False,
            "languageCode": language_code,
            "units": "METRIC",
        }
        if intermediates:
            payload["intermediates"] = [
                {"location": {"latLng": point.as_google_lat_lng()}}
                for point in intermediates
            ]
        if travel_mode == "drive" and traffic_aware:
            payload["routingPreference"] = "TRAFFIC_AWARE"

        response = self._request_json(
            ROUTES_COMPUTE_ROUTES_URL,
            method="POST",
            payload=payload,
            field_mask=ROUTE_FIELD_MASK,
        )
        routes = response.get("routes") or []
        if not routes:
            raise GoogleMapsRequestError("Google Routes에서 경로를 찾지 못했습니다.")
        route = routes[0]
        if not isinstance(route, dict):
            raise GoogleMapsRequestError("Google Routes 응답의 경로 형식이 올바르지 않습니다.")
        try:
            polyline = (route.get("polyline") or {}).get("encodedPolyline")
            return RouteResult(
                duration_seconds=_duration_seconds(route.get("duration")),
                distance_meters=int(route["distanceMeters"]),
                encoded_polyline=_optional_text(polyline),
            )
        except (KeyError, TypeError, ValueError) as error:
            raise GoogleMapsRequestError(
                "Google Routes 응답의 경로 형식이 올바르지 않습니다."
            ) from error

    def build_static_map_request(
        self,
        *,
        markers: tuple[StaticMapMarker, ...] = (),
        encoded_polyline: str | None = None,
        center: Coordinates | None = None,
        zoom: int | None = None,
        width: int = 600,
        height: int = 400,
        scale: int = 1,
        map_type: Literal["roadmap", "satellite", "terrain", "hybrid"] = "roadmap",
    ) -> Request:
        """백엔드 전용 Maps Static API 이미지 요청을 만든다.

        Static Maps API는 쿼리 문자열 키로 인증하므로, 이후 API 라우터에서
        ``request.full_url``을 반환하지 않는다. 대신 :meth:`fetch_static_map_image`를
        사용해 반환된 이미지 바이트를 프록시하여 브라우저가 키를 보지 않게 한다.
        """

        if not 1 <= width <= 640 or not 1 <= height <= 640:
            raise ValueError("정적 지도 크기는 가로·세로 각각 1에서 640 사이여야 합니다.")
        if scale not in (1, 2):
            raise ValueError("정적 지도 배율은 1 또는 2여야 합니다.")
        if map_type not in ("roadmap", "satellite", "terrain", "hybrid"):
            raise ValueError("지원하지 않는 정적 지도 종류입니다.")
        if zoom is not None and not 0 <= zoom <= 21:
            raise ValueError("지도 확대 수준은 0에서 21 사이여야 합니다.")
        if center is not None and zoom is None:
            raise ValueError("지도 중심 좌표를 지정하면 확대 수준도 지정하세요.")
        if center is None and not markers and not encoded_polyline:
            raise ValueError("지도 중심, 마커, 또는 경로 중 하나가 필요합니다.")
        if encoded_polyline is not None and len(encoded_polyline) > 8_000:
            raise ValueError("정적 지도 경로가 너무 깁니다. 경로를 나누어 표시하세요.")

        params: list[tuple[str, str]] = [
            ("size", f"{width}x{height}"),
            ("scale", str(scale)),
            ("maptype", map_type),
        ]
        if center is not None:
            params.append(("center", center.as_static_map_value()))
            params.append(("zoom", str(zoom)))
        for marker in markers:
            params.append(("markers", marker.as_static_map_value()))
        if encoded_polyline:
            # `urlencode`가 경로 구분 문자를 이스케이프하므로, Google 인코딩
            # 폴리라인에 예약 문자가 있어도 유효한 URL을 유지할 수 있다.
            params.append(("path", f"weight:5|color:0x3367D6FF|enc:{encoded_polyline}"))
        params.append(("key", self._api_key))
        return Request(f"{STATIC_MAP_URL}?{urlencode(params)}", method="GET")

    def fetch_static_map_image(self, **kwargs: Any) -> StaticMapImage:
        """이후 백엔드 프록시 응답에 쓸 정적 지도 이미지를 내려받는다."""

        request = self.build_static_map_request(**kwargs)
        content, content_type = self._request_bytes(request)
        if not content_type.startswith("image/"):
            raise GoogleMapsRequestError("Google Static Maps가 지도 이미지를 반환하지 않았습니다.")
        return StaticMapImage(content=content, content_type=content_type)

    def _request_json(
        self,
        url: str,
        *,
        method: Literal["GET", "POST"],
        field_mask: str,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """인증된 JSON Maps 요청 하나를 보내고 객체 응답을 해석한다."""

        headers = {
            "X-Goog-Api-Key": self._api_key,
            "X-Goog-FieldMask": field_mask,
            "Content-Type": "application/json",
        }
        body = json.dumps(payload).encode("utf-8") if payload is not None else None
        request = Request(url, data=body, headers=headers, method=method)
        content, _ = self._request_bytes(request)
        try:
            decoded = json.loads(content.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise GoogleMapsRequestError("Google Maps가 JSON 응답을 반환하지 않았습니다.") from error
        if not isinstance(decoded, dict):
            raise GoogleMapsRequestError("Google Maps 응답 형식이 올바르지 않습니다.")
        return decoded

    def _request_bytes(self, request: Request) -> tuple[bytes, str]:
        """오류 메시지에 키나 전체 URL을 노출하지 않고 요청을 실행한다."""

        try:
            with urlopen(request, timeout=self._timeout_seconds) as response:  # noqa: S310
                content = response.read()
                content_type = response.headers.get_content_type()
                return content, content_type
        except HTTPError as error:
            raise GoogleMapsRequestError(
                f"Google Maps API 요청이 거부되었습니다. (HTTP {error.code})"
            ) from error
        except (URLError, OSError, TimeoutError) as error:
            raise GoogleMapsRequestError(
                "Google Maps API에 연결하지 못했습니다. 잠시 후 다시 시도하세요."
            ) from error
