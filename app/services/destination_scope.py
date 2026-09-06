"""AI 장소 후보를 Google이 확인한 한 도시 안으로 제한한다.

정밀 행정 경계 다각형 데이터는 사용하지 않는다. 도시의 지도 표시 사각형은
검색 범위를 좁히는 용도로만 쓰고, 실제 소속은 구조화된 도시·국가·상위 행정구역
이름도 함께 비교한다. 주소 구성요소가 빠지면 추측하지 않고 후보를 거절한다.
숙소나 추가 API 키는 필요하지 않으며 도시 메타데이터는 생성 요청 안에서만 쓴다.
"""

from dataclasses import dataclass, replace
import unicodedata

from app.google_maps_client import GeoViewport, GoogleMapsClient, PlaceResult


_CITY_TYPES = ("locality", "postal_town")
_PARENT_TYPES = ("administrative_area_level_1", "administrative_area_level_2")


def _names(place: PlaceResult, component_type: str) -> frozenset[str]:
    """Google의 같은 종류 주소 구성요소에서 긴 이름·짧은 이름만 비교한다.

    표시 주소의 문자열 포함 여부나 번역을 추측하지 않는다. '오사카부' 안에
    있다는 이유로 사카이시까지 '오사카시'로 통과시키지 않기 위한 구분이다.
    """
    return frozenset(
        normalized
        for component in place.address_components
        if component_type in component.types
        for value in (component.long_text, component.short_text)
        if (normalized := " ".join(unicodedata.normalize("NFKC", value).casefold().split()))
    )


@dataclass(frozen=True)
class DestinationScope:
    """도시 장소 응답에서 얻은 검색 범위와 소속 비교 기준이다."""

    google_place_id: str
    display_name: str
    city_type: str
    city_names: frozenset[str]
    country_names: frozenset[str]
    parent_names: tuple[tuple[str, frozenset[str]], ...]
    viewport: GeoViewport

    @classmethod
    def from_city(cls, place: PlaceResult) -> "DestinationScope":
        """나라·도·식당 응답을 도시로 오인하거나 범위를 임의 확대하지 않는다."""
        city_type = next((kind for kind in _CITY_TYPES if kind in place.types and _names(place, kind)), None)
        countries = _names(place, "country")
        if (
            city_type is None or not countries or not place.google_place_id
            or place.viewport is None or place.coordinates is None
            or not place.viewport.contains(place.coordinates)
        ):
            raise ValueError("도시의 위치와 행정구역 정보를 확인할 수 없습니다.")
        return cls(
            google_place_id=place.google_place_id,
            display_name=place.display_name,
            city_type=city_type,
            city_names=_names(place, city_type),
            country_names=countries,
            parent_names=tuple((kind, names) for kind in _PARENT_TYPES if (names := _names(place, kind))),
            viewport=place.viewport,
        )

    def accepts(self, place: PlaceResult) -> bool:
        """좌표와 도시·국가·상위 행정구역을 모두 확인한 실제 장소만 통과시킨다."""
        return self.rejection_reason(place) is None

    def rejection_reason(self, place: PlaceResult) -> str | None:
        """검색 결과 없음과 언어·주소·좌표 검증 실패를 구분할 짧은 진단 코드이다."""
        if place.coordinates is None:
            return "missing_coordinates"
        if not self.viewport.contains(place.coordinates):
            return "outside_viewport"
        cities = _names(place, self.city_type)
        if not cities:
            return "missing_city"
        if not self.city_names.intersection(cities):
            return "city_name_mismatch"
        countries = _names(place, "country")
        if not countries:
            return "missing_country"
        if not self.country_names.intersection(countries):
            return "country_mismatch"
        for kind, names in self.parent_names:
            candidate_names = _names(place, kind)
            if not candidate_names:
                return "missing_parent_area"
            if not names.intersection(candidate_names):
                return "administrative_name_mismatch"
        return None

    def with_city_aliases(self, city: PlaceResult) -> "DestinationScope":
        """같은 Google 도시 ID의 번역명만 추가하고 허용 지역·주소 필수 조건은 유지한다."""
        translated = DestinationScope.from_city(city)
        if (
            translated.google_place_id != self.google_place_id
            or translated.city_type != self.city_type
            or not self.country_names.intersection(translated.country_names)
            or not self.viewport.contains(city.coordinates)
        ):
            raise ValueError("같은 도시의 언어별 주소 정보인지 확인할 수 없습니다.")
        translated_parents = dict(translated.parent_names)
        return replace(
            self,
            city_names=self.city_names | translated.city_names,
            country_names=self.country_names | translated.country_names,
            parent_names=tuple(
                (kind, names | translated_parents.get(kind, frozenset()))
                for kind, names in self.parent_names
            ),
        )


def resolve_destination_scope(maps: GoogleMapsClient, destination: str) -> DestinationScope:
    """사용자가 입력한 도시를 한 번 조회하고, 모호하거나 미확인인 도시는 거절한다."""
    scopes: dict[str, DestinationScope] = {}
    for candidate in maps.search_city(destination, language_code="ko"):
        try:
            scope = DestinationScope.from_city(candidate)
        except ValueError:
            continue
        # Google ID가 달라도 같은 행정구역과 같은 표시 범위를 가진 별칭 응답은
        # 하나로 취급한다. 이름만 같고 위치·국가·상위 지역이 다르면 합치지 않는다.
        if any(
            (scope.city_type, scope.city_names, scope.country_names, scope.parent_names, scope.viewport)
            == (existing.city_type, existing.city_names, existing.country_names, existing.parent_names, existing.viewport)
            for existing in scopes.values()
        ):
            continue
        scopes[scope.google_place_id] = scope
    if len(scopes) != 1:
        raise ValueError(
            "여행할 도시 범위를 하나로 확인하지 못했습니다. "
            "'오사카, 일본'처럼 도시와 국가를 입력하세요. 광역 지역이나 여러 도시는 지원하지 않습니다."
        )
    return next(iter(scopes.values()))
