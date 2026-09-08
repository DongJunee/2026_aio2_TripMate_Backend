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

# 광역자치단체 자체가 하나의 도시인 곳.
#
# [변경 사유] 도쿄도·서울특별시는 Google 이 locality 가 아니라
# administrative_area_level_1 로 준다. 그대로 두면 "도쿄" 여행을 아예 만들 수 없다.
#
# [변경 사유] 크기로 거르지 않는다. 도쿄도의 표시 범위는 266km 로 경기도(154km)·
# 후쿠오카현(139km)·오사카부(87km)보다 크다. 도쿄를 통과시키는 임계값은 그 셋을
# 함께 통과시킨다. types·primaryType·addressComponents 구성도 다섯 곳이 모두 같아
# 응답만으로는 갈라낼 신호가 없다.
#
# [변경 사유] 그래서 추측하지 않고 Google 장소 ID 로 명시한다. 표시 이름은 언어와
# 표기에 따라 달라지지만 ID 는 고정이다. 여기 없는 광역구역은 지금처럼 거절된다 —
# 새로 넣을 때는 그 안에서 하루 일정이 성립하는 '도시'인지 확인하고 한 줄 추가한다.
_METROPOLIS_CITY_TYPE = "administrative_area_level_1"
_METROPOLIS_PLACE_IDS = frozenset({
    "ChIJ51cu8IcbXWARiRtXIothAS4",  # 도쿄도 · Tokyo
    "ChIJzzlcLQGifDURm_JbQKHsEX4",  # 서울특별시 · Seoul
})

# 화면에 보여 줄 도시 이름.
#
# [변경 사유] Google 이 주는 이름은 행정구역 표기라 "도쿄도"·"서울특별시" 다.
# 사용자가 고를 때 읽는 이름으로는 어색하므로 표시용 이름을 따로 둔다.
#
# [변경 사유] 이 이름은 **표시에만** 쓴다. 여행 생성에 보내는 값은 Google 표기
# 그대로여야 도시를 다시 찾을 때 어긋나지 않는다. 두 값을 하나로 합치지 않는
# 이유가 이것이다.
#
# [변경 사유] 허용 목록과 따로 둔다. 표기를 다듬는 일과 도시로 인정하는 일은
# 다른 판단이고, 나중에 "오사카시 → 오사카" 처럼 locality 도시의 표기만 고치고
# 싶을 수 있다. 여기 없는 도시는 Google 이름을 그대로 쓰므로 빠뜨려도 안전하다.
_DISPLAY_LABELS = {
    "ChIJ51cu8IcbXWARiRtXIothAS4": "도쿄",
    "ChIJzzlcLQGifDURm_JbQKHsEX4": "서울",
}


def display_label(place: PlaceResult) -> str:
    """화면에 보여 줄 도시 이름. 정해 둔 표기가 없으면 Google 이름을 그대로 쓴다."""
    return _DISPLAY_LABELS.get(place.google_place_id) or place.display_name


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
        if (
            city_type is None
            # [변경 사유] 허용 목록에 있는 광역시만 그 광역구역 이름을 도시 이름으로
            # 삼는다. ID 를 먼저 보므로 오사카부·경기도는 여기 들어오지 못한다.
            and place.google_place_id in _METROPOLIS_PLACE_IDS
            and _METROPOLIS_CITY_TYPE in place.types
            and _names(place, _METROPOLIS_CITY_TYPE)
        ):
            city_type = _METROPOLIS_CITY_TYPE
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
            # [변경 사유] 도시 이름으로 쓴 종류는 상위 지역 검사에서 뺀다. 광역시를
            # 도시로 인정하면 city_type 과 _PARENT_TYPES 가 겹쳐 같은 이름을 두 번
            # 대조하게 된다. locality 도시는 city_type 이 _PARENT_TYPES 에 없으므로
            # 이 조건이 걸리지 않아 기존 동작 그대로다.
            parent_names=tuple(
                (kind, names) for kind in _PARENT_TYPES
                if kind != city_type and (names := _names(place, kind))
            ),
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

#0908(LSW)수정
# def resolve_destination_scope(maps: GoogleMapsClient, destination: str) -> DestinationScope:
#     """사용자가 입력한 도시를 한 번 조회하고, 모호하거나 미확인인 도시는 거절한다."""
#     scopes: dict[str, DestinationScope] = {}
#     for candidate in maps.search_city(destination, language_code="ko"):
#         try:
#             scope = DestinationScope.from_city(candidate)
#         except ValueError:
#             continue
#         # Google ID가 달라도 같은 행정구역과 같은 표시 범위를 가진 별칭 응답은
#         # 하나로 취급한다. 이름만 같고 위치·국가·상위 지역이 다르면 합치지 않는다.
#         if any(
#             (scope.city_type, scope.city_names, scope.country_names, scope.parent_names, scope.viewport)
#             == (existing.city_type, existing.city_names, existing.country_names, existing.parent_names, existing.viewport)
#             for existing in scopes.values()
#         ):
#             continue
#         scopes[scope.google_place_id] = scope
#     if len(scopes) != 1:
#         raise ValueError(
#             "여행할 도시 범위를 하나로 확인하지 못했습니다. "
#             "'오사카, 일본'처럼 도시와 국가를 입력하세요. 광역 지역이나 여러 도시는 지원하지 않습니다."
#         )
#     return next(iter(scopes.values()))

#0908(LSW)수정
def list_destination_candidates(
    maps: GoogleMapsClient, destination: str
) -> list[tuple[PlaceResult, DestinationScope]]:
    """입력한 이름으로 Google이 도시라고 확인한 후보만 중복 없이 돌려준다.

    [변경 사유] 검색 화면은 후보 목록이 필요하고 생성 경로는 하나로 좁혀야 한다.
    두 곳이 각자 목록을 만들면 검색에서 고를 수 있었던 도시를 생성에서 거절하는
    어긋남이 생긴다. 판정 기준을 한 곳에 두고 좁히는 규칙만 아래에서 따로 건다.
    표시에 필요한 원본 응답(PlaceResult)을 함께 돌려준다.
    DestinationScope 에 표시용 칸(주소·나라 원문)을 새로 넣으면 dataclass 의
    동등 비교가 바뀌고, 그 비교는 아래 중복 제거와 with_city_aliases 가 쓰고 있다.
    """
    scopes: dict[str, tuple[PlaceResult, DestinationScope]] = {}
    for candidate in maps.search_city(destination, language_code="ko"):
        try:
            scope = DestinationScope.from_city(candidate)
        except ValueError:
            continue
        # Google ID가 달라도 같은 행정구역과 같은 표시 범위를 가진 별칭 응답은
        # 하나로 취급한다. 이름만 같고 위치·국가·상위 지역이 다르면 합치지 않는다.
        # (기존 resolve_destination_scope 에 있던 규칙을 그대로 옮긴 것이다.)
        if any(
            (scope.city_type, scope.city_names, scope.country_names,
             scope.parent_names, scope.viewport)
            == (existing.city_type, existing.city_names, existing.country_names,
                existing.parent_names, existing.viewport)
            for _, existing in scopes.values()
        ):
            continue
        scopes[scope.google_place_id] = (candidate, scope)
    return list(scopes.values())


def resolve_destination_scope(maps: GoogleMapsClient, destination: str) -> DestinationScope:
    """사용자가 입력한 도시를 한 번 조회하고 모호하거나 미확인인 도시는 거절한다.

    [변경 사유] 본문을 list_destination_candidates 로 옮겼을 뿐, 시그니처와 예외
    문구는 그대로다. 호출처(maps.py · trips.py)와 기존 테스트가 이 함수의 동작에
    의존하고 있으므로 겉보기 동작을 바꾸지 않는다.
    """
    candidates = list_destination_candidates(maps, destination)
    if len(candidates) != 1:
        raise ValueError(
            "여행할 도시 범위를 하나로 확인하지 못했습니다. "
            "'오사카, 일본'처럼 도시와 국가를 입력하세요. 광역 지역이나 여러 도시는 지원하지 않습니다."
        )
    return candidates[0][1]