"""외부 API 없이 강도별 일정 배치, 현지 오전 9시, 엄격한 AI 응답 검증을 확인한다."""

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import unittest
from unittest.mock import patch
from uuid import UUID

from app.schemas import TripCreate
from app.services.itinerary_generation import (
    _generation_system_prompt,
    _normalise_days,
    generate_daily_itinerary_drafts,
)
from app.services.travel_preferences import daily_schedule_slots, is_departure_day, travel_preferences_text


class ItineraryLocalTimeTests(unittest.TestCase):
    """네트워크 생성만 대체하고 실제 일정 검증·현지 시간 조립을 실행한다."""

    def make_days(self, *travel_dates: str) -> list[dict]:
        """실제 일정 스키마가 허용하는 UUID를 가진 빈 DAY 목록을 만든다."""
        return [
            {"id": str(UUID(int=number)), "day_number": number, "travel_date": value, "items": []}
            for number, value in enumerate(travel_dates, start=1)
        ]

    def make_output(
        self, zone: str | None, day_count: int = 1, intensity: int = 3,
        departure_day_numbers: set[int] | None = None,
    ) -> dict:
        """모델은 시각 없이 활동·식사 칸의 검색어만 반환한다."""
        slot_ids = [f"activity_{index}" for index in range(1, intensity + 1)] + ["lunch", "dinner"]
        output = {"days": [
            {"day_number": number, "items": [
                {"slot_id": slot_id, "place_query": f"도쿄 DAY {number} {slot_id}", "notes": "가까운 장소 추천"}
                for slot_id in slot_ids
            ]} for number in range(1, day_count + 1)
        ]}
        for day in output["days"]:
            if day["day_number"] in (departure_day_numbers or set()):
                permitted = {"activity_1", "lunch"} if intensity >= 3 else {"lunch"}
                day["items"] = [item for item in day["items"] if item["slot_id"] in permitted]
        if zone is not None:
            output["timezone"] = zone
        return output

    def generate(self, output: dict, days: list[dict], trip: dict | None = None):
        """Gemini 호출만 대체하고 서비스의 공개 함수를 실행한다."""
        with patch("app.services.itinerary_generation._generate_gemini_json", return_value=output) as generate_json:
            result = generate_daily_itinerary_drafts(trip or {"destination": "여행지"}, days)
        generate_json.assert_called_once()
        return result

    def first_timed_items(self, items: list[dict]) -> list[dict]:
        """시간 미정 체크인을 제외하고 각 DAY에서 처음 시작하는 일정을 고른다."""
        first_by_day = {}
        for item in items:
            if item.get("start_at"):
                first_by_day.setdefault(item["trip_day_id"], item)
        return list(first_by_day.values())

    def test_trip_create_does_not_default_to_seoul(self):
        """시간대를 입력하지 않은 새 여행에 서울 시간대가 몰래 지정되지 않는다."""
        self.assertIsNone(TripCreate(title="하와이 여행", destination="호놀룰루").timezone)

    def test_generated_timezone_sets_local_nine_and_correct_utc(self):
        """모든 강도에서 서버 시간대와 무관하게 여행지 현지 오전 9시를 만든다."""
        cases = (
            ("Asia/Tokyo", "2026-09-10", 9, "2026-09-10T00:00:00+00:00"),
            ("Pacific/Honolulu", "2026-09-10", -10, "2026-09-10T19:00:00+00:00"),
            ("Europe/Paris", "2026-07-10", 2, "2026-07-10T07:00:00+00:00"),
            ("Europe/Paris", "2026-01-10", 1, "2026-01-10T08:00:00+00:00"),
        )
        for intensity in range(1, 6):
            for zone, travel_date, offset, expected_utc in cases:
                with self.subTest(intensity=intensity, zone=zone, date=travel_date):
                    result = self.generate(
                        self.make_output(zone, intensity=intensity), self.make_days(travel_date),
                        {"destination": "여행지", "travel_intensity": intensity},
                    )
                    start = datetime.fromisoformat(self.first_timed_items(result.items)[0]["start_at"])
                    self.assertEqual(result.timezone, zone)
                    self.assertEqual(start.date().isoformat(), travel_date)
                    self.assertEqual((start.hour, start.minute), (9, 0))
                    self.assertEqual(start.utcoffset(), timedelta(hours=offset))
                    self.assertEqual(start.astimezone(timezone.utc).isoformat(), expected_utc)

    def test_paris_dst_changes_preserve_nine_each_day(self):
        """파리 서머타임 전환일에도 각 DAY 첫 일정은 현지 오전 9시이다."""
        for dates, offsets in (
            (("2026-03-28", "2026-03-29", "2026-03-30"), (1, 2, 2)),
            (("2026-10-24", "2026-10-25", "2026-10-26"), (2, 1, 1)),
        ):
            result = self.generate(self.make_output("Europe/Paris", len(dates)), self.make_days(*dates))
            first_items = self.first_timed_items(result.items)
            self.assertEqual(len(first_items), len(dates))
            for item, value, offset in zip(first_items, dates, offsets):
                start = datetime.fromisoformat(item["start_at"])
                self.assertEqual(start.date().isoformat(), value)
                self.assertEqual((start.hour, start.minute), (9, 0))
                self.assertEqual(start.utcoffset(), timedelta(hours=offset))

    def test_explicit_timezone_accepts_missing_or_matching_model_timezone(self):
        """기존 여행의 시간대를 응답에서 생략하거나 동일하게 반환하면 허용한다."""
        for model_zone in (None, "Pacific/Honolulu"):
            result = self.generate(
                self.make_output(model_zone), self.make_days("2026-09-10"),
                {"destination": "호놀룰루", "timezone": "Pacific/Honolulu"},
            )
            self.assertEqual(result.timezone, "Pacific/Honolulu")

    def test_timezone_mismatch_missing_and_unknown_are_rejected(self):
        """미확인 시간대나 모델의 시간대 변경을 임의 기본값으로 대체하지 않는다."""
        for zone in (None, "", "Mars/Olympus", "+09:00"):
            with self.subTest(zone=zone), self.assertRaises(ValueError):
                self.generate(self.make_output(zone), self.make_days("2026-09-10"))
        for zone in ("Pacific/Honolulu", "Unknown/City"):
            with self.subTest(explicit=zone), self.assertRaises(ValueError):
                self.generate(
                    self.make_output("Asia/Tokyo"), self.make_days("2026-09-10"),
                    {"destination": "여행지", "timezone": zone},
                )

    def test_each_intensity_has_exact_activity_meal_and_rest_order(self):
        """사용자가 지정한 강도별 활동 개수와 식사·휴식 순서를 검증한다."""
        expected = {
            1: ["morning_leisure", "lunch", "activity_1", "rest_afternoon", "dinner", "rest_evening"],
            2: ["morning_leisure", "lunch", "activity_1", "rest_afternoon", "dinner", "activity_2"],
            3: ["activity_1", "lunch", "activity_2", "dinner", "activity_3"],
            4: ["activity_1", "lunch", "activity_2", "activity_3", "dinner", "activity_4"],
            5: ["activity_1", "lunch", "activity_2", "activity_3", "dinner", "activity_4", "activity_5"],
        }
        for intensity, expected_order in expected.items():
            with self.subTest(intensity=intensity):
                self.assertEqual([slot.slot_id for slot in daily_schedule_slots(intensity)], expected_order)
                result = self.generate(
                    self.make_output("Asia/Tokyo", 2, intensity), self.make_days("2026-09-10", "2026-09-11"),
                    {"destination": "도쿄", "travel_intensity": intensity},
                )
                for day_number in (1, 2):
                    day_items = [item for item in result.items if item["trip_day_id"] == str(UUID(int=day_number))]
                    timed = [item for item in day_items if item.get("start_at")]
                    self.assertEqual(len(timed), len(expected_order))
                    self.assertEqual(sum(item["item_type"] == "place" for item in day_items), intensity)
                    self.assertEqual(sum(item["item_type"] == "restaurant" for item in day_items), 2)
                    self.assertEqual(
                        [item["_place_query"].split()[-1] for item in timed if "_place_query" in item],
                        [slot_id for slot_id in expected_order if slot_id.startswith("activity_") or slot_id in {"lunch", "dinner"}],
                    )
                    previous_end = None
                    for item in timed:
                        start, end = datetime.fromisoformat(item["start_at"]), datetime.fromisoformat(item["end_at"])
                        self.assertLess(start, end)
                        if previous_end:
                            self.assertLessEqual(previous_end, start)
                        previous_end = end
                    meals = [item for item in timed if item["item_type"] == "restaurant"]
                    self.assertEqual([datetime.fromisoformat(item["start_at"]).hour for item in meals], [12, 18])

    def test_checkin_once_untimed_and_rest_placeholders_skip_places(self):
        """첫날 체크인은 미정이며 휴식·여유 시간에는 가짜 검색어·장소 ID가 없다."""
        result = self.generate(
            self.make_output("Asia/Tokyo", 2, 1), self.make_days("2026-09-10", "2026-09-11"),
            {"destination": "도쿄", "travel_intensity": 1},
        )
        checkins = [item for item in result.items if item["title"].startswith("체크인")]
        self.assertEqual(len(checkins), 1)
        self.assertEqual(result.items[0], checkins[0])
        self.assertEqual(checkins[0]["trip_day_id"], str(UUID(int=1)))
        self.assertNotIn("start_at", checkins[0])
        self.assertNotIn("end_at", checkins[0])
        for item in result.items:
            if item["item_type"] in {"hotel", "note"}:
                self.assertIs(item["_activity_only"], True)
                self.assertNotIn("_place_query", item)
                self.assertNotIn("place_id", item)
            else:
                self.assertIn("_place_query", item)
                self.assertNotIn("_activity_only", item)

    def test_model_order_does_not_change_day_or_slot_order(self):
        """모델의 DAY·활동 배열을 뒤집어도 서버의 일정 순서가 유지된다."""
        output = self.make_output("Asia/Tokyo", 2)
        expected = self.generate(output, self.make_days("2026-09-10", "2026-09-11"))
        output["days"].reverse()
        for day in output["days"]:
            day["items"].reverse()
        self.assertEqual(self.generate(output, self.make_days("2026-09-10", "2026-09-11")).items, expected.items)

    def test_missing_duplicate_extra_and_unknown_slots_are_rejected(self):
        """일부 칸을 누락하거나 중복·추가한 응답을 성공 처리하지 않는다."""
        base = self.make_output("Asia/Tokyo")
        missing, duplicate, extra, unknown = (deepcopy(base) for _ in range(4))
        missing["days"][0]["items"].pop()
        duplicate["days"][0]["items"][1]["slot_id"] = "activity_1"
        extra["days"][0]["items"].append({"slot_id": "activity_4", "place_query": "도쿄 센소지"})
        unknown["days"][0]["items"][0]["slot_id"] = "rest_evening"
        for output in (missing, duplicate, extra, unknown):
            with self.subTest(output=output), self.assertRaises(ValueError):
                self.generate(output, self.make_days("2026-09-10"))

    def test_model_cannot_choose_early_time_kind_or_internal_markers(self):
        """새벽 시각·행 종류·검색 우회 표시를 모델이 지정하면 거절한다."""
        for field, value in (("start_time", "01:00"), ("item_type", "hotel"), ("_activity_only", True)):
            output = self.make_output("Asia/Tokyo")
            output["days"][0]["items"][0][field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.generate(output, self.make_days("2026-09-10"))

    def test_model_value_types_are_validated(self):
        """문자열 대신 객체·숫자 또는 잘못된 DAY 번호를 받으면 거절한다."""
        for field, value in (("place_query", {}), ("place_query", 1234), ("notes", []), ("slot_id", [])):
            output = self.make_output("Asia/Tokyo")
            output["days"][0]["items"][0][field] = value
            with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                self.generate(output, self.make_days("2026-09-10"))
        for day_number in (True, 1.5, "1", 2):
            output = self.make_output("Asia/Tokyo")
            output["days"][0]["day_number"] = day_number
            with self.subTest(day_number=day_number), self.assertRaises(ValueError):
                self.generate(output, self.make_days("2026-09-10"))

    def test_malformed_days_and_non_place_queries_are_rejected(self):
        """DAY 누락·중복 및 실제 장소가 아닌 과정 검색어를 거절한다."""
        duplicate = self.make_output("Asia/Tokyo")
        duplicate["days"].append(deepcopy(duplicate["days"][0]))
        invalid = [{"timezone": "Asia/Tokyo"}, {"timezone": "Asia/Tokyo", "days": []}, duplicate]
        for query in (None, "", "오사카 공항 도착", "오사카 호텔 휴식", "x" * 201):
            output = self.make_output("Asia/Tokyo")
            output["days"][0]["items"][0]["place_query"] = query
            invalid.append(output)
        for output in invalid:
            with self.subTest(output=output), self.assertRaises(ValueError):
                self.generate(output, self.make_days("2026-09-10"))

    def test_preference_levels_reject_invalid_values(self):
        """범위 밖 강도나 문자열·불리언을 정수로 바꾸지 않는다."""
        for value in (0, 6, True, "3", 2.5):
            with self.subTest(intensity=value), self.assertRaises(ValueError):
                daily_schedule_slots(value)
            with self.subTest(budget=value), self.assertRaises(ValueError):
                travel_preferences_text({"budget_level": value})

    def test_provider_failure_does_not_create_fallback_items(self):
        """고정 휴식 시각표가 있어도 Gemini 실패 시 대체 여행을 만들지 않는다."""
        with patch(
            "app.services.itinerary_generation._generate_gemini_json", side_effect=RuntimeError("제공자 실패")
        ), self.assertRaises(ValueError):
            generate_daily_itinerary_drafts({"destination": "도쿄"}, self.make_days("2026-09-10"))

    def test_prompt_includes_party_budget_and_exact_slot_contract(self):
        """저장한 여행 조건이 현지 시각표와 추천 지침에 함께 반영된다."""
        contexts = _normalise_days(self.make_days("2026-09-10"))
        prompt = _generation_system_prompt(
            {"destination": "호놀룰루", "travel_party": "family_with_children", "travel_intensity": 5, "budget_level": 1}, contexts
        )
        for expected in ("호놀룰루", "IANA", "09:00", "아이 동반 가족", "최대한 절약", "activity_5", "lunch", "dinner"):
            self.assertIn(expected, prompt)
        self.assertNotIn("activity_6", prompt)
        self.assertNotIn('"start_time"', prompt)
        self.assertNotIn('"item_type"', prompt)
        explicit = _generation_system_prompt({"destination": "파리", "timezone": "Europe/Paris"}, contexts)
        self.assertIn("지정된 IANA 시간대: Europe/Paris", explicit)

    def test_defaults_and_party_do_not_change_selected_intensity(self):
        """기존 여행 기본값은 중간 단계이고 동행 구성으로 활동 개수를 바꾸지 않는다."""
        text = travel_preferences_text({})
        for expected in ("미선택", "여행 강도: 3/5", "여행 경비 수준: 3/5"):
            self.assertIn(expected, text)
        text = travel_preferences_text({"travel_party": "senior_couple", "travel_intensity": 5})
        for expected in ("시니어 부부", "하루 관광·활동 5개", "나이만으로"):
            self.assertIn(expected, text)

    def test_departure_day_overrides_every_intensity_and_one_day_trip(self):
        """하루 여행도 출국일로 처리하고 강도와 무관하게 오후·밤 관광을 금지한다."""
        for intensity in range(1, 6):
            with self.subTest(intensity=intensity):
                result = self.generate(
                    self.make_output("Asia/Tokyo", intensity=intensity, departure_day_numbers={1}),
                    self.make_days("2026-09-10"),
                    {"destination": "오사카", "travel_intensity": intensity, "end_date": "2026-09-10"},
                )
                self.assertEqual(len(result.items), 5)
                self.assertEqual(sum(item["item_type"] == "place" for item in result.items), 1 if intensity >= 3 else 0)
                self.assertEqual(sum(item["item_type"] == "restaurant" for item in result.items), 1)
                self.assertFalse(any(item["item_type"] == "hotel" for item in result.items))
                self.assertFalse(any(item["title"].startswith("체크인") for item in result.items))
                self.assertEqual(datetime.fromisoformat(result.items[0]["start_at"]).hour, 9)
                for item in result.items:
                    end = datetime.fromisoformat(item["end_at"])
                    self.assertLessEqual((end.hour, end.minute), (18, 0))
                    if "_place_query" in item:
                        self.assertLessEqual((end.hour, end.minute), (13, 0))
                departure = result.items[-1]
                self.assertEqual(departure["start_at"], departure["end_at"])
                self.assertEqual(departure["estimated_stay_minutes"], 0)

    def test_only_actual_end_date_gets_departure_slots(self):
        """여러 날 중 실제 종료일만 줄이고 첫날 및 중간 날의 일반 일정은 유지한다."""
        days = self.make_days("2026-09-10", "2026-09-11", "2026-09-12", "2026-09-13")
        result = self.generate(
            self.make_output("Asia/Tokyo", 4, 5, {4}), days,
            {"destination": "오사카", "travel_intensity": 5, "end_date": "2026-09-13"},
        )
        self.assertEqual(len(result.items), 27)
        for day_number in (1, 2, 3, 4):
            items = [item for item in result.items if item["trip_day_id"] == str(UUID(int=day_number))]
            activities = [item for item in items if item["item_type"] == "place"]
            self.assertEqual(len(activities), 1 if day_number == 4 else 5)
            self.assertEqual(any(item["title"].startswith("18:00 출국") for item in items), day_number == 4)

    def test_partial_generation_does_not_treat_last_requested_day_as_departure(self):
        """일부 DAY만 생성할 때 배열의 마지막 날짜를 여행 종료일로 잘못 간주하지 않는다."""
        day = self.make_days("2026-09-11")[0]
        day["day_number"] = 2
        output = self.make_output("Asia/Tokyo", intensity=5)
        output["days"][0]["day_number"] = 2
        result = self.generate(
            output, [day], {"destination": "오사카", "travel_intensity": 5, "end_date": "2026-09-13"}
        )
        self.assertEqual(len(result.items), 7)
        self.assertEqual(sum(item["item_type"] == "place" for item in result.items), 5)
        self.assertFalse(any(item["title"].startswith("18:00 출국") for item in result.items))

    def test_departure_day_rejects_evening_model_slots(self):
        """모델이 출국일에도 일반 날의 저녁·밤 추천을 반환하면 전체 생성을 거부한다."""
        with self.assertRaises(ValueError):
            self.generate(
                self.make_output("Asia/Tokyo", intensity=5), self.make_days("2026-09-10"),
                {"destination": "오사카", "travel_intensity": 5, "end_date": "2026-09-10"},
            )

    def test_departure_notes_do_not_claim_verified_airport_or_route(self):
        """출국 안내는 실제 장소·항공권·교통시간으로 위장하지 않는 서버 안내이다."""
        result = self.generate(
            self.make_output("Asia/Tokyo", intensity=5, departure_day_numbers={1}),
            self.make_days("2026-09-10"),
            {"destination": "오사카", "travel_intensity": 5, "end_date": "2026-09-10"},
        )
        transfer, preparation, departure = result.items[-3:]
        self.assertEqual(datetime.fromisoformat(transfer["start_at"]).hour, 13)
        self.assertEqual(datetime.fromisoformat(transfer["end_at"]).hour, 15)
        self.assertIn("계산한 이동시간이 아닙니다", transfer["notes"])
        self.assertEqual(datetime.fromisoformat(preparation["start_at"]).hour, 15)
        self.assertEqual(datetime.fromisoformat(preparation["end_at"]).hour, 18)
        self.assertIn("예약을 확인한 정보가 아닙니다", departure["notes"])
        for item in (transfer, preparation, departure):
            self.assertEqual(item["item_type"], "note")
            self.assertIs(item["_activity_only"], True)
            self.assertNotIn("place_id", item)
            self.assertNotIn("_place_query", item)

    def test_departure_time_uses_destination_offset_and_dst(self):
        """18시 출국도 UTC나 서버시각이 아닌 해당 날짜의 현지 시각으로 저장한다."""
        for zone, travel_date, offset in (
            ("Asia/Tokyo", "2026-09-10", 9),
            ("Pacific/Honolulu", "2026-09-10", -10),
            ("Europe/Paris", "2026-03-29", 2),
            ("Europe/Paris", "2026-10-25", 1),
        ):
            with self.subTest(zone=zone, travel_date=travel_date):
                result = self.generate(
                    self.make_output(zone, departure_day_numbers={1}), self.make_days(travel_date),
                    {"destination": "여행지", "end_date": travel_date},
                )
                departure_at = datetime.fromisoformat(result.items[-1]["start_at"])
                self.assertEqual(departure_at.date().isoformat(), travel_date)
                self.assertEqual((departure_at.hour, departure_at.minute), (18, 0))
                self.assertEqual(departure_at.utcoffset(), timedelta(hours=offset))

    def test_prompt_names_city_boundary_and_day_specific_departure_slots(self):
        """도시 제한과 실제 마지막 날의 축소된 슬롯을 프롬프트에 명시한다."""
        prompt = _generation_system_prompt(
            {"destination": "오사카", "travel_intensity": 5, "end_date": "2026-09-11"},
            _normalise_days(self.make_days("2026-09-10", "2026-09-11")),
        )
        for expected in ("선택한 여행 도시 안", "근교나 다른 도시 방문은 금지", "DAY 1 (일반 일정)", "DAY 2 (출국일", "2026-09-11"):
            self.assertIn(expected, prompt)
        second_day_slots = prompt.split("DAY 2 (출국일", 1)[1].split("activity는", 1)[0]
        self.assertIn("activity_1", second_day_slots)
        self.assertIn("lunch", second_day_slots)
        self.assertNotIn("dinner", second_day_slots)
        self.assertNotIn("activity_2", second_day_slots)

    def test_departure_day_requires_valid_actual_end_date(self):
        """종료일이 없으면 추측하지 않고, 잘못된 값은 조용히 무시하지 않는다."""
        from datetime import date

        travel_date = date(2026, 9, 10)
        self.assertFalse(is_departure_day(travel_date, None))
        self.assertFalse(is_departure_day(travel_date, "2026-09-11"))
        self.assertTrue(is_departure_day(travel_date, travel_date))
        with self.assertRaises(ValueError):
            is_departure_day(travel_date, "잘못된 날짜")


if __name__ == "__main__":
    unittest.main()
