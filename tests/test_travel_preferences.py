"""외부 서비스를 호출하지 않고 여행 조건의 검증·저장·장소 연결을 확인한다."""

import os
import unittest
from unittest.mock import MagicMock, patch
from uuid import UUID

from fastapi import HTTPException
from pydantic import ValidationError

from app.deps import CurrentUser
from app.gemini_client import _travel_generation_inputs
from app.google_maps_client import Coordinates, PlaceResult
from app.routers import trips
from app.schemas import TripCreate, TripUpdate


class TravelPreferenceTests(unittest.TestCase):
    """새 여행과 기존 여행 수정에 같은 범위·동행 구성 규칙이 적용된다."""

    def test_defaults_and_all_party_values(self):
        """기존 호출은 기본 강도·경비로 생성하고 모든 DB 동행 값을 허용한다."""
        for party in (
            "unspecified", "solo", "couple", "friends", "family",
            "family_with_children", "with_parents", "senior_couple", "other",
        ):
            with self.subTest(party=party):
                payload = TripCreate(title="테스트 여행", travel_party=party)
                self.assertEqual(payload.travel_intensity, 3)
                self.assertEqual(payload.budget_level, 3)
                self.assertEqual(payload.travel_party, party)
        self.assertEqual(TripCreate(title="이전 호출").travel_party, "unspecified")
        self.assertEqual(TripUpdate().model_dump(exclude_unset=True), {})

    def test_invalid_preferences_are_rejected_for_create_and_update(self):
        """숫자 범위 밖, 문자열, 소수, bool, null 및 잘못된 동행 구성은 거부한다."""
        invalids = [
            {field: value}
            for field in ("travel_intensity", "budget_level")
            for value in (0, 6, -1, 1.5, "3", True, None)
        ] + [{"travel_party": "unknown"}, {"travel_party": None}]
        for values in invalids:
            for model in (TripCreate, TripUpdate):
                with self.subTest(values=values, model=model.__name__), self.assertRaises(ValidationError):
                    model.model_validate({"title": "검증 테스트", **values})

    def test_settings_update_only_saves_supplied_trip_fields(self):
        """슬라이더 저장은 소유자 확인 뒤 조건만 저장하며 기존 일정을 재생성하지 않는다."""
        trip_id = UUID(int=50)
        user = CurrentUser(id=str(UUID(int=70)), email="test@example.com", token="test")
        client = MagicMock()
        client.table.return_value.update.return_value.eq.return_value.execute.return_value.data = [{"id": str(trip_id)}]
        dashboard = {"trip": {"id": str(trip_id), "travel_intensity": 2, "budget_level": 4}, "days": []}
        with (
            patch.object(trips, "get_user_client", return_value=client),
            patch.object(trips, "_owned_trip", return_value={"id": str(trip_id)}) as owned,
            patch.object(trips, "trip_dashboard", return_value=dashboard),
            patch.object(trips, "generate_daily_itinerary_drafts") as generate,
        ):
            result = trips.update_trip(trip_id, TripUpdate(travel_intensity=2, budget_level=4), user)
        owned.assert_called_once_with(client, trip_id)
        client.table.assert_called_once_with("trips")
        saved = client.table.return_value.update.call_args.args[0]
        self.assertEqual(saved["travel_intensity"], 2)
        self.assertEqual(saved["budget_level"], 4)
        self.assertEqual(set(saved), {"travel_intensity", "budget_level", "updated_at"})
        generate.assert_not_called()
        self.assertIs(result, dashboard)

    def test_non_owner_cannot_change_preferences(self):
        """소유권 검증이 실패하면 update 요청도 수행하지 않는다."""
        client = MagicMock()
        with (
            patch.object(trips, "get_user_client", return_value=client),
            patch.object(trips, "_owned_trip", side_effect=HTTPException(status_code=404)),
            self.assertRaises(HTTPException),
        ):
            trips.update_trip(
                UUID(int=50), TripUpdate(travel_intensity=1),
                CurrentUser(id=str(UUID(int=70)), email="test@example.com", token="test"),
            )
        client.table.assert_not_called()

    def test_rest_rows_skip_google_and_repeated_queries_are_reused(self):
        """휴식은 검색하지 않고 동일 검색어만 재사용하며 내부 표시는 DB 행에서 제거한다."""
        place = PlaceResult(
            google_place_id="test-place", display_name="실제 카페",
            formatted_address="도쿄", coordinates=Coordinates(35.68, 139.76),
            google_rating=None, google_rating_count=None, primary_type="cafe",
            types=("cafe",), google_maps_uri=None,
        )
        maps = MagicMock()
        maps.search_places.return_value = [place]
        client = MagicMock()
        rows = [
            {"title": "체크인", "item_type": "hotel", "_activity_only": True},
            {"title": "오전 여유 시간", "item_type": "note", "_activity_only": True},
            {"title": "검색 전", "item_type": "place", "_place_query": "시내 카페", "start_at": "2026-09-10T09:00:00+09:00"},
            {"title": "호텔 휴식", "item_type": "hotel", "_activity_only": True},
            {"title": "다른 날짜", "item_type": "place", "_place_query": "시내 카페", "start_at": "2026-09-11T09:00:00+09:00"},
        ]
        with (
            patch.object(trips.GoogleMapsClient, "from_environment", return_value=maps),
            patch.object(trips, "resolve_destination_scope") as resolve_scope,
            patch.object(trips, "_cache_google_place", return_value={"id": "stored-place"}) as cache,
        ):
            resolve_scope.return_value.accepts.return_value = True
            resolved = trips._resolve_initial_itinerary_places(client, {"destination": "도쿄"}, rows)
        maps.search_places.assert_called_once_with(
            "시내 카페 도쿄", max_results=5, language_code="ko",
            location_restriction=resolve_scope.return_value.viewport, include_region_metadata=True,
        )
        cache.assert_called_once()
        self.assertEqual(len(resolved), 5)
        for row in resolved:
            self.assertNotIn("_activity_only", row)
            self.assertNotIn("_place_query", row)
        self.assertEqual(resolved[2]["place_id"], resolved[4]["place_id"])
        self.assertNotEqual(resolved[2]["start_at"], resolved[4]["start_at"])
        self.assertNotIn("place_id", resolved[0])
        self.assertIn("_activity_only", rows[0])

    def test_placeholder_cannot_bypass_real_place_resolution(self):
        """실제 장소 행을 휴식이라고 표시해 Google 검증을 건너뛰지 못하게 한다."""
        with (
            patch.object(trips.GoogleMapsClient, "from_environment"),
            patch.object(trips, "resolve_destination_scope"),
            self.assertRaises(HTTPException) as raised,
        ):
            trips._resolve_initial_itinerary_places(
                MagicMock(), {"destination": "도쿄"},
                [{"item_type": "place", "_activity_only": True}],
            )
        self.assertEqual(raised.exception.status_code, 502)

    def test_chat_receives_latest_preferences_and_local_times(self):
        """설정 저장 이후 채팅에는 최신 조건과 UTC를 변환한 현지 시각을 전달한다."""
        trip = {
            "title": "여행", "destination": "도쿄", "timezone": "Asia/Tokyo",
            "travel_party": "family_with_children", "travel_intensity": 1, "budget_level": 5,
        }
        days = [{"day_number": 1, "travel_date": "2026-09-10", "items": [
            {"title": "카페", "start_at": "2026-09-10T00:00:00Z"},
            {"title": "체크인", "start_at": None},
        ]}]
        with patch.dict(os.environ, {"GEMINI_API_KEY": "test-no-network"}):
            _, _, prompt, _ = _travel_generation_inputs(trip, days, [], "다음 장소는?")
        self.assertIn("09:00", prompt)
        self.assertNotIn("00:00:00Z", prompt)
        self.assertIn("시간 미정", prompt)
        self.assertIn("현재 저장된 여행 조건", prompt)
        self.assertIn("아이 동반 가족", prompt)
        self.assertIn("여행 강도: 1/5", prompt)
        self.assertIn("여행 경비 수준: 5/5", prompt)


if __name__ == "__main__":
    unittest.main()
