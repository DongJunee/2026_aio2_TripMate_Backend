"""DB와 외부 API를 대체해 여행 시간대 저장 및 기간 변경 시 현지 시각을 검증한다."""

from copy import deepcopy
from datetime import timedelta
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch
from uuid import UUID

from fastapi import HTTPException

from app.deps import CurrentUser
from app.routers import trips
from app.schemas import TripCreate


class TripTimezoneStorageTests(unittest.TestCase):
    """여행 생성과 날짜 변경이 현지 오전 9시를 보존하는지 확인한다."""

    def test_date_shift_preserves_local_time_across_dst(self):
        """UTC 저장값의 날짜를 옮길 때 파리 서머타임 전환도 함께 반영한다."""
        cases = (
            ("2026-03-28T08:00:00Z", 1, "Europe/Paris", "2026-03-29T09:00:00+02:00"),
            ("2026-03-29T07:00:00Z", -1, "Europe/Paris", "2026-03-28T09:00:00+01:00"),
            ("2026-10-24T07:00:00Z", 1, "Europe/Paris", "2026-10-25T09:00:00+01:00"),
            ("2026-10-25T08:00:00Z", -1, "Europe/Paris", "2026-10-24T09:00:00+02:00"),
            ("2026-09-10T00:00:00Z", 1, "Asia/Tokyo", "2026-09-11T09:00:00+09:00"),
            ("2026-09-10T19:00:00Z", 1, "Pacific/Honolulu", "2026-09-11T09:00:00-10:00"),
            ("2026-03-28T09:00:00", 1, "Europe/Paris", "2026-03-29T09:00:00+02:00"),
        )
        for value, delta, zone, expected in cases:
            with self.subTest(value=value, delta=delta, zone=zone):
                self.assertEqual(
                    trips._shift_datetime(value, timedelta(days=delta), zone), expected
                )

    def test_creation_saves_generated_timezone_with_matching_local_items(self):
        """시간대 입력 없이 생성한 여행과 일정에 Gemini가 확인한 현지 시간대를 저장한다."""
        stored = {}
        trip_id = str(UUID(int=100))
        user_id = str(UUID(int=200))
        client = MagicMock()

        def table(name):
            """Supabase의 insert 실행값만 기록하고 외부 DB 연결을 하지 않는다."""
            query = MagicMock()

            def insert(values):
                stored[name] = deepcopy(values)
                rows = [{**values, "id": trip_id}] if name == "trips" else deepcopy(values)
                return SimpleNamespace(execute=lambda: SimpleNamespace(data=rows))

            query.insert.side_effect = insert
            return query

        client.table.side_effect = table
        output = {
            "timezone": "Pacific/Honolulu",
            "days": [{
                "day_number": 1,
                "items": [{
                    "slot_id": slot_id,
                    "place_query": "호놀룰루 와이키키 카페",
                    "notes": "여행 조건에 맞는 장소 후보",
                } for slot_id in (
                    "activity_1", "lunch",
                )],
            }],
        }

        def resolve_places(_client, trip_values, drafts):
            """실제 장소 검색 대신 시간대 전달과 검증된 일정 값을 확인한다."""
            self.assertEqual(trip_values["timezone"], "Pacific/Honolulu")
            resolved = []
            for draft in drafts:
                row = dict(draft)
                if row.pop("_place_query", None):
                    row["place_id"] = str(UUID(int=300))
                row.pop("_activity_only", None)
                resolved.append(row)
            return resolved

        payload = TripCreate(
            title="하와이 여행", destination="호놀룰루",
            start_date="2026-09-10", end_date="2026-09-10",
            travel_party="family_with_children", travel_intensity=5, budget_level=1,
        )
        with (
            patch.object(trips, "get_user_client", return_value=client),
            patch("app.services.itinerary_generation._generate_gemini_json", return_value=output),
            patch.object(trips, "_resolve_initial_itinerary_places", side_effect=resolve_places),
            patch.object(trips, "trip_dashboard", return_value={"trip": {"id": trip_id}, "days": []}),
        ):
            result = trips.create_my_trip(
                payload, CurrentUser(id=user_id, email="test@example.com", token="test-token")
            )

        self.assertEqual(stored["trips"]["timezone"], "Pacific/Honolulu")
        self.assertEqual(stored["trips"]["user_id"], user_id)
        self.assertEqual(stored["trips"]["travel_party"], "family_with_children")
        self.assertEqual(stored["trips"]["travel_intensity"], 5)
        self.assertEqual(stored["trips"]["budget_level"], 1)
        first_timed = next(row for row in stored["itinerary_items"] if row.get("start_at"))
        self.assertEqual(first_timed["start_at"], "2026-09-10T09:00:00-10:00")
        self.assertEqual(stored["itinerary_items"][0]["trip_id"], trip_id)
        self.assertEqual(stored["itinerary_items"][0]["trip_day_id"], stored["trip_days"][0]["id"])
        # 하루 여행도 출국일 정책이 우선이다: 활동·점심 + 출국 준비 안내 3개.
        self.assertEqual(result["initial_itinerary_count"], 5)
        self.assertTrue(all(row["end_at"] <= "2026-09-10T18:00:00-10:00" for row in stored["itinerary_items"]))

    def test_failed_timezone_validation_prevents_trip_insert(self):
        """AI가 시간대를 누락하면 여행 또는 일차 DB 행을 저장하지 않는다."""
        client = MagicMock()
        payload = TripCreate(
            title="하와이 여행", destination="호놀룰루",
            start_date="2026-09-10", end_date="2026-09-10",
        )
        with (
            patch.object(trips, "get_user_client", return_value=client),
            patch("app.services.itinerary_generation._generate_gemini_json", return_value={"days": []}),
            patch.object(trips, "_resolve_initial_itinerary_places") as resolve_places,
            self.assertRaises(HTTPException) as raised,
        ):
            trips.create_my_trip(
                payload, CurrentUser(id=str(UUID(int=200)), email="test@example.com", token="test-token")
            )
        self.assertEqual(raised.exception.status_code, 502)
        client.table.assert_not_called()
        resolve_places.assert_not_called()


if __name__ == "__main__":
    unittest.main()
