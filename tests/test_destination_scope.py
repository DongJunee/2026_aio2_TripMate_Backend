"""외부 API 호출 없이 도시 범위 검증과 Places 검색 제한 요청을 확인한다."""

from dataclasses import replace
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch
from urllib.parse import parse_qs, urlparse
from uuid import UUID

from fastapi import HTTPException

from app.deps import CurrentUser
from app.google_maps_client import (
    AddressComponent,
    Coordinates,
    GeoViewport,
    GoogleMapsClient,
    GoogleMapsRequestError,
    PlaceResult,
)
from app.routers import trips
from app.schemas import TripCreate
from app.services.destination_scope import DestinationScope, resolve_destination_scope


def make_place(
    *,
    place_id: str = "osaka-cafe",
    city: str = "Osaka",
    country: str = "JP",
    admin: str = "Osaka Prefecture",
    latitude: float = 34.6937,
    longitude: float = 135.5023,
    is_city: bool = False,
    city_type: str = "locality",
) -> PlaceResult:
    """실제 장소 검색 없이 도시·국가·상위 행정구역이 있는 후보를 만든다."""
    components = (
        AddressComponent(city, city, (city_type, "political")),
        AddressComponent(admin, admin, ("administrative_area_level_1", "political")),
        AddressComponent(country, country, ("country", "political")),
    )
    # 실제 행정 경계가 아닌 사각형에는 이웃 도시가 포함될 수 있다.
    # 교토·사카이도 일부러 포함시켜 좌표만으로 통과시키는 구현을 검출한다.
    viewport = GeoViewport(Coordinates(34.0, 135.0), Coordinates(36.0, 136.0))
    return PlaceResult(
        google_place_id=place_id,
        display_name=city if is_city else "시험 카페",
        formatted_address=f"{city}, {admin}, {country}",
        coordinates=Coordinates(latitude, longitude),
        google_rating=None,
        google_rating_count=None,
        primary_type=city_type if is_city else "cafe",
        types=(city_type, "political") if is_city else ("cafe", "food"),
        google_maps_uri=None,
        address_components=components,
        viewport=viewport if is_city else None,
    )


def city_payload() -> dict:
    """Google 응답의 공개 구조만 재현하며 API 키나 실제 사용자 정보는 쓰지 않는다."""
    return {
        "id": "osaka-city",
        "displayName": {"text": "Osaka"},
        "formattedAddress": "Osaka, Osaka Prefecture, JP",
        "location": {"latitude": 34.6937, "longitude": 135.5023},
        "primaryType": "locality",
        "types": ["locality", "political"],
        "addressComponents": [
            {"longText": "Osaka", "shortText": "Osaka", "types": ["locality", "political"]},
            {"longText": "Osaka Prefecture", "shortText": "Osaka Prefecture", "types": ["administrative_area_level_1", "political"]},
            {"longText": "Japan", "shortText": "JP", "types": ["country", "political"]},
        ],
        "viewport": {
            "low": {"latitude": 34.0, "longitude": 135.0},
            "high": {"latitude": 36.0, "longitude": 136.0},
        },
    }


def fukuoka_place(*, is_city: bool, english: bool = False, place_id: str | None = None) -> PlaceResult:
    """실제 장애에서 확인한 한글 도시·영문 후보 주소의 형태를 재현한다.

    장소 ID와 좌표 범위는 독립 테스트용 값이며 실제 Google 응답을 새로 요청하지 않는다.
    """
    city_name = "Fukuoka" if english else "후쿠오카시"
    admin_name = "Fukuoka" if english else "후쿠오카현"
    return replace(
        make_place(
            place_id=place_id or ("fukuoka-city" if is_city else "fukuoka-cafe"),
            city=city_name, admin=admin_name, is_city=is_city,
            latitude=33.59, longitude=130.40,
        ),
        address_components=(
            AddressComponent(city_name, city_name, ("locality", "political")),
            AddressComponent(admin_name, admin_name, ("administrative_area_level_1", "political")),
            AddressComponent("Japan" if english and is_city else "일본", "JP", ("country", "political")),
        ),
        viewport=GeoViewport(Coordinates(33.3, 130.1), Coordinates(33.9, 130.8)) if is_city else None,
    )


class GeoViewportTests(unittest.TestCase):
    """검색 사각형의 경계 및 날짜변경선을 넘는 경도를 검증한다."""

    def test_normal_rectangle_includes_edges_and_excludes_outside(self):
        viewport = GeoViewport(Coordinates(34, 135), Coordinates(36, 136))
        for coordinate in (Coordinates(34, 135), Coordinates(36, 136), Coordinates(35, 135.5)):
            with self.subTest(coordinate=coordinate):
                self.assertTrue(viewport.contains(coordinate))
        for coordinate in (Coordinates(33.99, 135.5), Coordinates(36.01, 135.5), Coordinates(35, 134.99), Coordinates(35, 136.01)):
            with self.subTest(coordinate=coordinate):
                self.assertFalse(viewport.contains(coordinate))

    def test_antimeridian_rectangle_includes_both_sides(self):
        viewport = GeoViewport(Coordinates(-20, 170), Coordinates(-10, -170))
        for longitude in (170, 175, 180, -180, -175, -170):
            with self.subTest(longitude=longitude):
                self.assertTrue(viewport.contains(Coordinates(-15, longitude)))
        for longitude in (0, 169.99, -169.99):
            with self.subTest(longitude=longitude):
                self.assertFalse(viewport.contains(Coordinates(-15, longitude)))
        self.assertFalse(viewport.contains(Coordinates(-21, 175)))

    def test_google_rectangle_preserves_low_high_coordinates(self):
        viewport = GeoViewport(Coordinates(-20, 170), Coordinates(-10, -170))
        self.assertEqual(viewport.as_google_rectangle(), {
            "low": {"latitude": -20, "longitude": 170},
            "high": {"latitude": -10, "longitude": -170},
        })


class DestinationScopeTests(unittest.TestCase):
    """좌표 사각형 안에서도 여행 도시가 다른 후보는 통과하지 못한다."""

    def setUp(self):
        self.city = make_place(place_id="osaka-city", is_city=True)
        self.scope = DestinationScope.from_city(self.city)

    def test_matching_city_country_admin_and_coordinates_are_accepted(self):
        self.assertTrue(self.scope.accepts(make_place()))

    def test_kyoto_inside_wide_viewport_is_rejected(self):
        self.assertFalse(self.scope.accepts(make_place(
            city="Kyoto", admin="Kyoto Prefecture", latitude=35.0116, longitude=135.7681,
        )))

    def test_sakai_in_same_prefecture_is_rejected(self):
        self.assertFalse(self.scope.accepts(make_place(
            city="Sakai", latitude=34.5733, longitude=135.483,
        )))

    def test_same_city_name_in_different_country_is_rejected(self):
        self.assertFalse(self.scope.accepts(make_place(country="US")))

    def test_different_upper_administrative_area_is_rejected(self):
        self.assertFalse(self.scope.accepts(make_place(admin="Other Prefecture")))

    def test_matching_address_outside_viewport_is_rejected(self):
        self.assertFalse(self.scope.accepts(make_place(latitude=35, longitude=139)))

    def test_missing_coordinate_or_structured_address_is_rejected(self):
        candidate = make_place()
        self.assertFalse(self.scope.accepts(replace(candidate, coordinates=None)))
        self.assertFalse(self.scope.accepts(replace(candidate, address_components=())))

    def test_missing_city_country_or_required_admin_is_rejected(self):
        candidate = make_place()
        for field in ("locality", "country", "administrative_area_level_1"):
            with self.subTest(field=field):
                incomplete = tuple(part for part in candidate.address_components if field not in part.types)
                self.assertFalse(self.scope.accepts(replace(candidate, address_components=incomplete)))

    def test_country_and_prefecture_alone_do_not_define_a_city(self):
        for kind in ("country", "administrative_area_level_1"):
            with self.subTest(kind=kind):
                broad_region = replace(
                    self.city,
                    primary_type=kind,
                    types=(kind, "political"),
                    address_components=tuple(part for part in self.city.address_components if "locality" not in part.types),
                )
                with self.assertRaises(ValueError):
                    DestinationScope.from_city(broad_region)

    def test_city_without_viewport_is_rejected(self):
        with self.assertRaises(ValueError):
            DestinationScope.from_city(replace(self.city, viewport=None))

    def test_city_missing_country_or_coordinate_is_rejected(self):
        without_country = tuple(part for part in self.city.address_components if "country" not in part.types)
        for city in (replace(self.city, address_components=without_country), replace(self.city, coordinates=None)):
            with self.subTest(city=city):
                with self.assertRaises(ValueError):
                    DestinationScope.from_city(city)

    def test_city_center_outside_its_viewport_is_rejected(self):
        with self.assertRaises(ValueError):
            DestinationScope.from_city(replace(self.city, coordinates=Coordinates(35, 140)))

    def test_postal_town_is_supported_as_city_identity(self):
        city = make_place(city="Reading", country="GB", admin="England", is_city=True, city_type="postal_town")
        candidate = make_place(city="Reading", country="GB", admin="England", city_type="postal_town")
        self.assertTrue(DestinationScope.from_city(city).accepts(candidate))

    def test_city_lookup_requires_one_unambiguous_city(self):
        maps = MagicMock(spec=GoogleMapsClient)
        for candidates in ([], [self.city, make_place(place_id="kyoto-city", city="Kyoto", admin="Kyoto Prefecture", is_city=True)]):
            with self.subTest(count=len(candidates)):
                maps.search_city.return_value = candidates
                with self.assertRaises(ValueError):
                    resolve_destination_scope(maps, "오사카")

    def test_duplicate_results_for_same_city_do_not_create_false_ambiguity(self):
        maps = MagicMock(spec=GoogleMapsClient)
        maps.search_city.return_value = [self.city, replace(self.city, google_place_id="same-city-alias")]
        scope = resolve_destination_scope(maps, "오사카")
        self.assertTrue(scope.accepts(make_place()))
        maps.search_city.assert_called_once()

    def test_same_city_name_with_different_extent_still_requires_clarification(self):
        maps = MagicMock(spec=GoogleMapsClient)
        maps.search_city.return_value = [self.city, replace(
            self.city,
            google_place_id="same-name-other-region",
            viewport=GeoViewport(Coordinates(34, 135), Coordinates(37, 137)),
        )]
        with self.assertRaises(ValueError):
            resolve_destination_scope(maps, "오사카")


class DiacriticNotationTests(unittest.TestCase):
    """같은 지역의 발음부호 표기 차이만 흡수하고 다른 지역은 계속 거절한다."""

    def setUp(self):
        # Google 실측: 나트랑 도시 조회는 ko·en 모두 'Khanh Hoa' 를 주는데,
        # 일부 장소 응답은 같은 성을 'Khánh Hòa' 로 준다.
        # make_place 의 기본 표시 범위는 오사카권이라 나트랑 좌표를 담지 못한다.
        self.city = replace(
            make_place(
                place_id="nha-trang-city", city="Nha Trang", country="VN", admin="Khanh Hoa",
                latitude=12.2388, longitude=109.1967, is_city=True,
            ),
            viewport=GeoViewport(Coordinates(11.0, 108.0), Coordinates(13.0, 110.0)),
        )
        self.scope = DestinationScope.from_city(self.city)

    def test_same_area_with_vietnamese_diacritics_is_accepted(self):
        place = make_place(
            place_id="nha-trang-beach", city="Nha Trang", country="VN", admin="Khánh Hòa",
            latitude=12.2388, longitude=109.1967,
        )
        self.assertIsNone(self.scope.rejection_reason(place))

    def test_diacritics_on_both_sides_still_match(self):
        scope = DestinationScope.from_city(replace(
            self.city,
            address_components=tuple(
                AddressComponent("Khánh Hòa", "Khánh Hòa", ("administrative_area_level_1", "political"))
                if "administrative_area_level_1" in part.types else part
                for part in self.city.address_components
            ),
        ))
        plain = make_place(
            place_id="nha-trang-beach", city="Nha Trang", country="VN", admin="Khanh Hoa",
            latitude=12.2388, longitude=109.1967,
        )
        self.assertIsNone(scope.rejection_reason(plain))

    def test_different_city_in_same_area_is_still_rejected(self):
        # 실측된 '북나트랑' 사례. 성은 같지만 도시가 다르므로 통과하면 안 된다.
        place = make_place(
            place_id="bac-nha-trang", city="Bac Nha Trang", country="VN", admin="Khánh Hòa",
            latitude=12.2388, longitude=109.1967,
        )
        self.assertEqual(self.scope.rejection_reason(place), "city_name_mismatch")

    def test_kana_voicing_marks_are_not_folded_away(self):
        """라틴 문자 밖에서는 같은 처리가 뜻을 바꾸므로 적용하지 않는다."""
        scope = DestinationScope.from_city(make_place(
            place_id="kana-city", city="がっこう", country="JP", admin="テスト県", is_city=True,
        ))
        # 좌표는 make_place 기본값이라 표시 범위 안에 있고, 이름만 다르다.
        self.assertEqual(
            scope.rejection_reason(make_place(city="かっこう", country="JP", admin="テスト県")),
            "city_name_mismatch",
        )


class DestinationCityAliasTests(unittest.TestCase):
    """Google의 같은 도시 ID에서 확인한 번역만 허용하고 지역 제한을 유지한다."""

    def setUp(self):
        self.korean_city = fukuoka_place(is_city=True)
        self.english_city = fukuoka_place(is_city=True, english=True)
        self.english_cafe = fukuoka_place(is_city=False, english=True)
        self.scope = DestinationScope.from_city(self.korean_city)

    def test_observed_korean_city_and_english_place_match_after_verified_alias(self):
        self.assertFalse(self.scope.accepts(self.english_cafe))
        enriched = self.scope.with_city_aliases(self.english_city)
        self.assertTrue(enriched.accepts(self.english_cafe))
        self.assertTrue(enriched.accepts(fukuoka_place(is_city=False)))
        self.assertFalse(self.scope.accepts(self.english_cafe))

    def test_english_kyoto_still_fails_city_comparison(self):
        enriched = self.scope.with_city_aliases(self.english_city)
        # 좌표는 일부러 후쿠오카 안에 두어 사각형 검증만으로 통과시키지 못하게 한다.
        foreign_city_place = replace(self.english_cafe, address_components=(
            AddressComponent("Kyoto", "Kyoto", ("locality", "political")),
            AddressComponent("Fukuoka", "Fukuoka", ("administrative_area_level_1", "political")),
            AddressComponent("Japan", "JP", ("country", "political")),
        ))
        self.assertFalse(enriched.accepts(foreign_city_place))

    def test_translated_parent_is_checked_even_when_city_label_matches(self):
        candidate = fukuoka_place(is_city=False)
        components = tuple(
            AddressComponent("Fukuoka", "Fukuoka", part.types)
            if "administrative_area_level_1" in part.types else part
            for part in candidate.address_components
        )
        candidate = replace(candidate, address_components=components)
        self.assertEqual(self.scope.rejection_reason(candidate), "administrative_name_mismatch")
        self.assertTrue(self.scope.with_city_aliases(self.english_city).accepts(candidate))

    def test_alias_with_other_google_place_id_is_rejected(self):
        with self.assertRaises(ValueError):
            self.scope.with_city_aliases(replace(self.english_city, google_place_id="another-city"))

    def test_alias_with_other_country_is_rejected(self):
        components = tuple(
            AddressComponent("United States", "US", part.types) if "country" in part.types else part
            for part in self.english_city.address_components
        )
        with self.assertRaises(ValueError):
            self.scope.with_city_aliases(replace(self.english_city, address_components=components))

    def test_alias_with_other_city_component_type_is_rejected(self):
        components = tuple(
            AddressComponent(part.long_text, part.short_text, ("postal_town", "political"))
            if "locality" in part.types else part
            for part in self.english_city.address_components
        )
        with self.assertRaises(ValueError):
            self.scope.with_city_aliases(replace(
                self.english_city, address_components=components,
                primary_type="postal_town", types=("postal_town", "political"),
            ))

    def test_alias_never_expands_original_viewport(self):
        alias = replace(
            self.english_city,
            viewport=GeoViewport(Coordinates(30, 125), Coordinates(36, 140)),
        )
        enriched = self.scope.with_city_aliases(alias)
        self.assertEqual(enriched.viewport, self.scope.viewport)
        self.assertFalse(enriched.accepts(replace(self.english_cafe, coordinates=Coordinates(34.5, 131))))


class PlacesRegionRequestTests(unittest.TestCase):
    """HTTP 호출만 대체하여 실제 요청 JSON과 장소 응답 해석을 확인한다."""

    def setUp(self):
        self.maps = GoogleMapsClient("unit-test-key")

    def test_response_keeps_structured_address_and_viewport(self):
        result = PlaceResult.from_google_payload(city_payload())
        self.assertEqual(result.address_components[0].long_text, "Osaka")
        self.assertEqual(result.address_components[-1].short_text, "JP")
        self.assertEqual(result.viewport.low, Coordinates(34, 135))
        self.assertEqual(result.viewport.high, Coordinates(36, 136))

    def test_existing_place_response_without_region_fields_remains_usable(self):
        payload = city_payload()
        payload.pop("addressComponents")
        payload.pop("viewport")
        result = PlaceResult.from_google_payload(payload)
        self.assertEqual(result.address_components, ())
        self.assertIsNone(result.viewport)
        self.assertEqual(result.google_place_id, "osaka-city")

    def test_scoped_search_sends_restriction_and_region_field_mask(self):
        viewport = GeoViewport(Coordinates(34, 135), Coordinates(36, 136))
        with patch.object(self.maps, "_request_json", return_value={"places": []}) as request:
            self.maps.search_places(
                "오사카 카페", max_results=5, language_code="ko",
                location_restriction=viewport, include_region_metadata=True,
            )
        options = request.call_args.kwargs
        self.assertEqual(options["payload"]["locationRestriction"], {"rectangle": viewport.as_google_rectangle()})
        self.assertNotIn("locationBias", options["payload"])
        self.assertEqual(options["payload"]["textQuery"], "오사카 카페")
        self.assertEqual(options["payload"]["pageSize"], 5)
        self.assertIn("places.addressComponents", options["field_mask"].split(","))

    def test_city_search_requests_metadata_needed_to_verify_scope(self):
        with patch.object(self.maps, "_request_json", return_value={"places": [city_payload()]}) as request:
            results = self.maps.search_city("오사카", language_code="ko")
        options = request.call_args.kwargs
        self.assertEqual(options["payload"]["textQuery"], "오사카")
        self.assertEqual(options["payload"]["languageCode"], "ko")
        fields = options["field_mask"].split(",")
        self.assertIn("places.addressComponents", fields)
        self.assertIn("places.viewport", fields)
        self.assertEqual(results[0].google_place_id, "osaka-city")

    def test_unrestricted_manual_search_keeps_legacy_response_compatible(self):
        payload = city_payload()
        payload.pop("addressComponents")
        payload.pop("viewport")
        with patch.object(self.maps, "_request_json", return_value={"places": [payload]}) as request:
            results = self.maps.search_places("오사카 카페")
        options = request.call_args.kwargs
        self.assertNotIn("locationRestriction", options["payload"])
        self.assertNotIn("places.addressComponents", options["field_mask"].split(","))
        self.assertEqual(results[0].address_components, ())
        self.assertEqual(results[0].coordinates, Coordinates(34.6937, 135.5023))

    def test_city_details_requests_same_id_in_english_without_rating_fields(self):
        with patch.object(self.maps, "_request_json", return_value=city_payload()) as request:
            city = self.maps.get_city_details("osaka-city", language_code="en")
        url = urlparse(request.call_args.args[0])
        options = request.call_args.kwargs
        self.assertTrue(url.path.endswith("/places/osaka-city"))
        self.assertEqual(parse_qs(url.query)["languageCode"], ["en"])
        self.assertEqual(options["method"], "GET")
        fields = set(options["field_mask"].split(","))
        self.assertTrue({"id", "types", "addressComponents"}.issubset(fields))
        self.assertFalse({"rating", "userRatingCount", "reviews"}.intersection(fields))
        self.assertEqual(city.google_place_id, "osaka-city")


class TripScopeRoutingTests(unittest.TestCase):
    """라우터가 도시 안 후보만 저장하고 범위 미확인 시 생성을 중단하는지 확인한다."""

    def test_second_in_city_candidate_is_selected_and_repeated_query_reused(self):
        client = MagicMock()
        maps = MagicMock(spec=GoogleMapsClient)
        city = make_place(place_id="osaka-city", is_city=True)
        outside = make_place(
            place_id="kyoto-cafe", city="Kyoto", admin="Kyoto Prefecture", latitude=35.0116, longitude=135.7681,
        )
        inside = make_place(place_id="osaka-cafe")
        maps.search_city.return_value = [city]
        maps.search_places.return_value = [outside, inside]
        drafts = [
            {"_place_query": "카페", "item_type": "cafe", "start_at": "2026-09-10T09:00:00+09:00"},
            {"_place_query": "카페", "item_type": "cafe", "start_at": "2026-09-11T09:00:00+09:00"},
        ]
        with (
            patch.object(trips.GoogleMapsClient, "from_environment", return_value=maps),
            patch.object(trips, "_cache_google_place", return_value={"id": "stored-osaka-cafe"}) as cache,
        ):
            resolved = trips._resolve_initial_itinerary_places(client, {"destination": "오사카"}, drafts)
        maps.search_city.assert_called_once_with("오사카", language_code="ko")
        maps.get_city_details.assert_not_called()
        maps.search_places.assert_called_once_with(
            "카페 오사카", max_results=trips._ITINERARY_PLACE_CANDIDATES, language_code="ko",
            location_restriction=city.viewport, include_region_metadata=True,
        )
        cache.assert_called_once_with(client, inside.as_place_row())
        self.assertEqual(len(resolved), 2)
        for row in resolved:
            self.assertEqual(row["place_id"], "stored-osaka-cafe")
            self.assertEqual(row["source"], "ai_recommendation")
            self.assertNotIn("_place_query", row)
        self.assertEqual(resolved[1]["start_at"], drafts[1]["start_at"])

    def test_no_in_city_candidate_prevents_trip_and_place_writes(self):
        client = MagicMock()
        maps = MagicMock(spec=GoogleMapsClient)
        maps.search_city.return_value = [make_place(place_id="osaka-city", is_city=True)]
        maps.get_city_details.return_value = make_place(place_id="osaka-city", is_city=True)
        maps.search_places.return_value = [make_place(
            place_id="kyoto-cafe", city="Kyoto", admin="Kyoto Prefecture", latitude=35.0116, longitude=135.7681,
        )]
        generated = SimpleNamespace(timezone="Asia/Tokyo", items=[{
            "_place_query": "카페", "trip_day_id": str(UUID(int=1)),
            "item_type": "cafe", "start_at": "2026-09-10T09:00:00+09:00",
            "end_at": "2026-09-10T10:00:00+09:00",
        }])
        payload = TripCreate(
            title="오사카 여행", destination="오사카",
            start_date="2026-09-10", end_date="2026-09-10",
        )
        with (
            patch.object(trips, "get_user_client", return_value=client),
            patch.object(trips, "generate_daily_itinerary_drafts", return_value=generated),
            patch.object(trips.GoogleMapsClient, "from_environment", return_value=maps),
            patch.object(trips, "_cache_google_place") as cache,
            self.assertRaises(HTTPException) as raised,
        ):
            trips.create_my_trip(payload, CurrentUser(
                id=str(UUID(int=2)), email="scope@example.com", token="unit-test-token",
            ))
        self.assertIn(raised.exception.status_code, (422, 502))
        client.table.assert_not_called()
        cache.assert_not_called()
        maps.search_places.assert_called()

    def test_rejected_candidates_are_summarised_for_debug_logging(self):
        """검증 실패 진단 로그가 실패 경로에서 예외를 만들지 않는지 확인한다.

        이 로그는 후보가 전멸했을 때만 실행되므로, 여기서 터지면 422 로 끝났을
        요청이 500 이 된다. 사유별 개수만으로는 '다른 도시 추천' 과 '주소 표기
        불일치' 를 구분할 수 없어 이 요약이 원인 확인의 유일한 근거다.
        """
        scope = DestinationScope.from_city(make_place(place_id="osaka-city", is_city=True))
        kyoto = make_place(
            place_id="kyoto-cafe", city="Kyoto", admin="Kyoto Prefecture",
            latitude=35.0116, longitude=135.7681,
        )
        summary = trips._candidate_origin(scope, kyoto)
        self.assertEqual(summary["city"], "Kyoto")
        self.assertEqual(summary["area"], "Kyoto Prefecture")
        self.assertEqual(summary["reason"], "city_name_mismatch")
        self.assertEqual(summary["name"], kyoto.display_name)

    def test_debug_logging_runs_when_every_candidate_is_rejected(self):
        maps = MagicMock(spec=GoogleMapsClient)
        maps.search_city.return_value = [make_place(place_id="osaka-city", is_city=True)]
        maps.get_city_details.return_value = make_place(place_id="osaka-city", is_city=True)
        maps.search_places.return_value = [make_place(
            place_id="kyoto-cafe", city="Kyoto", admin="Kyoto Prefecture",
            latitude=35.0116, longitude=135.7681,
        )]
        with (
            patch.object(trips.GoogleMapsClient, "from_environment", return_value=maps),
            patch.object(trips, "_cache_google_place") as cache,
            self.assertLogs(trips.LOGGER, level="DEBUG") as logs,
            self.assertRaises(HTTPException) as raised,
        ):
            trips._resolve_initial_itinerary_places(
                MagicMock(), {"destination": "오사카"}, [{"_place_query": "카페", "item_type": "cafe"}],
            )
        self.assertEqual(raised.exception.status_code, 422)
        self.assertTrue(any("검색어=" in line and "city_name_mismatch" in line for line in logs.output))
        cache.assert_not_called()

    def test_fukuoka_alias_is_loaded_once_and_reused_for_later_queries(self):
        maps = MagicMock(spec=GoogleMapsClient)
        maps.search_city.return_value = [fukuoka_place(is_city=True)]
        maps.get_city_details.return_value = fukuoka_place(is_city=True, english=True)
        maps.search_places.side_effect = [
            [fukuoka_place(is_city=False, english=True, place_id="fukuoka-cafe")],
            [fukuoka_place(is_city=False, english=True, place_id="fukuoka-food")],
        ]
        drafts = [
            {"_place_query": "카페", "item_type": "cafe"},
            {"_place_query": "식당", "item_type": "restaurant"},
        ]
        with (
            patch.object(trips.GoogleMapsClient, "from_environment", return_value=maps),
            patch.object(trips, "_cache_google_place", side_effect=[{"id": "stored-cafe"}, {"id": "stored-food"}]) as cache,
        ):
            result = trips._resolve_initial_itinerary_places(MagicMock(), {"destination": "후쿠오카"}, drafts)
        maps.get_city_details.assert_called_once_with("fukuoka-city", language_code="en")
        self.assertEqual(maps.search_city.call_count, 1)
        self.assertEqual(maps.search_places.call_count, 2)
        self.assertEqual(cache.call_count, 2)
        self.assertEqual([row["place_id"] for row in result], ["stored-cafe", "stored-food"])

    def test_failed_english_city_lookup_keeps_unverified_place_blocked(self):
        maps = MagicMock(spec=GoogleMapsClient)
        maps.search_city.return_value = [fukuoka_place(is_city=True)]
        maps.search_places.return_value = [fukuoka_place(is_city=False, english=True)]
        maps.get_city_details.side_effect = GoogleMapsRequestError("시험용 조회 실패")
        with (
            patch.object(trips.GoogleMapsClient, "from_environment", return_value=maps),
            patch.object(trips, "_cache_google_place") as cache,
            self.assertRaises(HTTPException) as raised,
        ):
            trips._resolve_initial_itinerary_places(
                MagicMock(), {"destination": "후쿠오카"}, [{"_place_query": "카페", "item_type": "cafe"}],
            )
        self.assertIn(raised.exception.status_code, (422, 502))
        maps.get_city_details.assert_called_once_with("fukuoka-city", language_code="en")
        cache.assert_not_called()

    def test_empty_search_results_do_not_trigger_english_city_lookup(self):
        maps = MagicMock(spec=GoogleMapsClient)
        maps.search_city.return_value = [fukuoka_place(is_city=True)]
        maps.search_places.return_value = []
        with (
            patch.object(trips.GoogleMapsClient, "from_environment", return_value=maps),
            patch.object(trips, "_cache_google_place") as cache,
            self.assertRaises(HTTPException),
        ):
            trips._resolve_initial_itinerary_places(
                MagicMock(), {"destination": "후쿠오카"}, [{"_place_query": "카페", "item_type": "cafe"}],
            )
        maps.get_city_details.assert_not_called()
        cache.assert_not_called()

    def test_outside_viewport_does_not_trigger_english_city_lookup(self):
        maps = MagicMock(spec=GoogleMapsClient)
        maps.search_city.return_value = [fukuoka_place(is_city=True)]
        maps.search_places.return_value = [replace(
            fukuoka_place(is_city=False, english=True), coordinates=Coordinates(35.0, 135.0),
        )]
        with (
            patch.object(trips.GoogleMapsClient, "from_environment", return_value=maps),
            patch.object(trips, "_cache_google_place") as cache,
            self.assertRaises(HTTPException),
        ):
            trips._resolve_initial_itinerary_places(
                MagicMock(), {"destination": "후쿠오카"}, [{"_place_query": "카페", "item_type": "cafe"}],
            )
        maps.get_city_details.assert_not_called()
        cache.assert_not_called()


if __name__ == "__main__":
    unittest.main()
