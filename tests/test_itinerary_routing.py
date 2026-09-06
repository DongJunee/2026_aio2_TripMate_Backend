"""외부 API 없이 좌표 기반 날짜 재배치와 기존 시간표 보존을 검증한다."""

from copy import deepcopy
import unittest

from app.services.itinerary_routing import group_nearby_itinerary_places, _distance


class ItineraryRoutingTests(unittest.TestCase):
    def row(self, day, hour, place, item_type="place", **extra):
        """시간 칸과 장소가 연결된 추천 행을 만든다."""
        return dict(trip_day_id=str(day), start_at=f"2026-09-0{day}T{hour:02}:00:00+09:00",
                    end_at=f"2026-09-0{day}T{hour + 1:02}:00:00+09:00",
                    item_type=item_type, place_id=place, title=place, notes=f"추천 {place}",
                    source="ai_recommendation", is_fixed=False, **extra)

    def test_groups_two_distant_neighbourhoods_and_keeps_slots(self):
        """각 날짜에 섞인 두 지역이 분리되고 현지 시각과 추천 이유는 보존된다."""
        coordinates = {"a": (35, 135), "b": (35.001, 135),
                       "c": (35.1, 135), "d": (35.101, 135)}
        rows = [self.row(1, 9, "a"), self.row(1, 14, "c"),
                self.row(2, 9, "b"), self.row(2, 14, "d")]
        original = deepcopy(rows)
        result = group_nearby_itinerary_places(rows, coordinates)
        clusters = {frozenset(row["place_id"] for row in result if row["trip_day_id"] == day)
                    for day in ("1", "2")}
        self.assertEqual(clusters, {frozenset(("a", "b")), frozenset(("c", "d"))})
        for before, after in zip(rows, result):
            for key in ("start_at", "end_at", "trip_day_id", "item_type"):
                self.assertEqual(before[key], after[key])
            self.assertEqual(after["notes"], f"추천 {after['place_id']}")
        self.assertEqual(rows, original)
        self.assertEqual(result, group_nearby_itinerary_places(rows, coordinates))

    def test_fixed_meals_departure_and_missing_coordinates(self):
        """식사 종류와 출국일 칸 수, 고정 장소·좌표 없는 안내를 유지한다."""
        rows = [self.row(1, 9, "a"), self.row(1, 12, "l1", "restaurant"),
                self.row(1, 18, "d1", "restaurant"), self.row(2, 9, "b"),
                self.row(2, 12, "l2", "restaurant"), self.row(2, 18, "airport", "note")]
        rows[0]["is_fixed"] = True
        coordinates = {"a": (35, 135), "b": (35.1, 135), "l1": (35.1, 135),
                       "l2": (35, 135), "d1": (35, 135)}
        result = group_nearby_itinerary_places(rows, coordinates)
        self.assertEqual(result[0], rows[0])
        self.assertEqual(result[-1], rows[-1])
        self.assertEqual(result[2]["place_id"], "d1")
        self.assertEqual({result[1]["place_id"], result[4]["place_id"]}, {"l1", "l2"})
        self.assertEqual([r["start_at"] for r in rows], [r["start_at"] for r in result])

    def test_one_day_route_gets_shorter(self):
        """하루 안에서 되돌아가는 방문 순서도 개선한다."""
        coordinates = {str(i): (35 + i / 100, 135) for i in range(4)}
        rows = [self.row(1, hour, place) for hour, place in zip((9, 11, 14, 16), ("0", "3", "1", "2"))]
        result = group_nearby_itinerary_places(rows, coordinates)
        def length(items):
            return sum(_distance(coordinates[a["place_id"]], coordinates[b["place_id"]])
                       for a, b in zip(items, items[1:]))
        self.assertLess(length(result), length(rows))
        self.assertEqual(sorted(r["place_id"] for r in result), ["0", "1", "2", "3"])

    def test_empty_and_unknown_places_are_unchanged(self):
        self.assertEqual(group_nearby_itinerary_places([], {}), [])
        rows = [self.row(1, 9, "unknown")]
        self.assertEqual(group_nearby_itinerary_places(rows, {}), rows)


if __name__ == "__main__":
    unittest.main()
