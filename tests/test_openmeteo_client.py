"""Open-Meteo 응답 변환을 외부 네트워크 없이 검증한다."""

import json
import unittest
from unittest.mock import patch

from app.google_maps_client import Coordinates
from app.openmeteo_client import OpenMeteoClient


class FakeResponse:
    def __init__(self, payload):
        self.payload = json.dumps(payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        return self.payload


class OpenMeteoClientTests(unittest.TestCase):
    @patch("app.openmeteo_client.urlopen")
    def test_daily_response_is_normalized_for_frontend(self, urlopen):
        urlopen.return_value = FakeResponse(
            {
                "daily": {
                    "time": ["2026-09-08"],
                    "weather_code": [61],
                    "temperature_2m_min": [18.2],
                    "temperature_2m_max": [24.7],
                    "precipitation_probability_max": [68],
                }
            }
        )

        result = OpenMeteoClient().get_daily_forecasts(Coordinates(37.5665, 126.978))

        self.assertEqual(
            result,
            [
                {
                    "date": "2026-09-08",
                    "label": "약한 비",
                    "min_celsius": 18.2,
                    "max_celsius": 24.7,
                    "precipitation_percent": 68,
                }
            ],
        )
        request = urlopen.call_args.args[0]
        self.assertIn("api.open-meteo.com/v1/forecast", request.full_url)
        self.assertIn("forecast_days=16", request.full_url)
        self.assertNotIn("appid=", request.full_url)


if __name__ == "__main__":
    unittest.main()
