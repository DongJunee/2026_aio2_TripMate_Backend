"""대시보드의 구간별 자동 이동수단과 예보 범위를 외부 API 없이 검증한다."""

import unittest
from unittest.mock import MagicMock, patch

from fastapi import HTTPException

from app.routers import maps


class DashboardRoutePlanTests(unittest.TestCase):
    def markers(self):
        """세 장소로 이어지는 하루 마커를 만든다."""
        return [
            {"itinerary_item_id": str(index), "title": str(index), "latitude": 35 + index, "longitude": 135}
            for index in range(3)
        ]

    def test_walk_under_twenty_minutes_else_available_faster_transport(self):
        """짧은 구간은 걷고 긴 구간은 이용 가능한 대중교통·차 중 빠른 것을 고른다."""
        def route(_maps, pair, mode):
            pair_number = int(pair[0]["itinerary_item_id"])
            values = {
                (0, "walk"): 600,
                (1, "walk"): 1800,
                (1, "transit"): 900,
                (1, "drive"): 480,
            }
            seconds = values[(pair_number, mode)]
            return {"travel_mode": mode, "duration_seconds": seconds,
                    "distance_meters": seconds, "encoded_polyline": f"{pair_number}-{mode}"}

        with patch.object(maps, "_route_for_markers", side_effect=route):
            result = maps._automatic_route_plan(MagicMock(), self.markers())
        self.assertEqual([leg["travel_mode"] for leg in result["legs"]], ["walk", "drive"])
        self.assertEqual(result["total_duration_seconds"], 1080)
        self.assertEqual(result["unknown_leg_count"], 0)
        self.assertEqual(len(result["route_segments"]), 2)

    def test_unavailable_long_route_is_excluded_from_total(self):
        """긴 도보 뒤 대중교통과 차가 모두 실패하면 확인 안 됨으로 제외한다."""
        def route(_maps, _pair, mode):
            if mode == "walk":
                return {"travel_mode": mode, "duration_seconds": 1800,
                        "distance_meters": 2000, "encoded_polyline": "walk"}
            raise HTTPException(status_code=502, detail="경로 없음")

        with patch.object(maps, "_route_for_markers", side_effect=route):
            result = maps._automatic_route_plan(MagicMock(), self.markers()[:2])
        self.assertEqual(result["legs"][0]["status"], "unknown")
        self.assertEqual(result["total_duration_seconds"], 0)
        self.assertEqual(result["total_distance_meters"], 0)
        self.assertEqual(result["unknown_leg_count"], 1)

<<<<<<< HEAD
    def test_weather_outside_forecast_window_does_not_call_openweather(self):
        """장기 여행에는 정확하지 않은 예상 날씨 대신 예보 전을 표시한다.

        OpenWeather 무료 예보는 5일까지만 답한다. 그 밖의 날짜는 호출해도 쓸 값이
        오지 않으므로, 클라이언트를 만들지도 않는지까지 확인한다.
        """
        with patch.object(maps, "OpenWeatherClient") as weather_client:
            result = maps._weather_for_day(
                {"timezone": "Asia/Seoul"},
                {"travel_date": "2099-01-01"},
                self.markers()[:1],
            )
        self.assertEqual(result, {"status": "pending", "label": "예보 전"})
        weather_client.from_environment.assert_not_called()
=======
    def test_weather_outside_forecast_range_does_not_call_weather_api(self):
        """장기 여행에는 정확하지 않은 예상 날씨 대신 예보 전을 표시한다."""
        result = maps._weather_for_day(
            {"timezone": "Asia/Seoul"},
            {"travel_date": "2099-01-01"},
            self.markers()[:1],
        )
        self.assertEqual(result, {"status": "pending", "label": "예보 전"})
>>>>>>> 2c16d5b (OpenMeteoChange)


if __name__ == "__main__":
    unittest.main()
