"""백엔드에서만 사용하는 Open-Meteo 일별 예보 클라이언트이다.

Open-Meteo의 일별 예보 응답을 여행 화면에서 쓰기 좋은 최저·최고 기온,
강수 확률, 한국어 날씨 설명으로 묶어 반환한다. Open-Meteo 공개 예보 API는
API 키가 필요하지 않으므로 환경 변수나 비밀 키를 프론트엔드에 전달하지 않는다.
"""

import json
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from app.google_maps_client import Coordinates


OPEN_METEO_FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
DAILY_VARIABLES = (
    "weather_code",
    "temperature_2m_min",
    "temperature_2m_max",
    "precipitation_probability_max",
)
FORECAST_DAYS = 16

# Open-Meteo의 weather_code는 WMO 날씨 코드이다.
WMO_WEATHER_LABELS = {
    0: "맑음",
    1: "대체로 맑음",
    2: "부분적으로 흐림",
    3: "흐림",
    45: "안개",
    48: "착빙 안개",
    51: "약한 이슬비",
    53: "이슬비",
    55: "강한 이슬비",
    56: "약한 어는 이슬비",
    57: "강한 어는 이슬비",
    61: "약한 비",
    63: "비",
    65: "강한 비",
    66: "약한 어는 비",
    67: "강한 어는 비",
    71: "약한 눈",
    73: "눈",
    75: "강한 눈",
    77: "싸락눈",
    80: "약한 소나기",
    81: "소나기",
    82: "강한 소나기",
    85: "약한 눈 소나기",
    86: "강한 눈 소나기",
    95: "뇌우",
    96: "약한 우박을 동반한 뇌우",
    99: "강한 우박을 동반한 뇌우",
}


class OpenMeteoError(RuntimeError):
    """완료할 수 없는 Open-Meteo 요청의 기본 오류이다."""


class OpenMeteoRequestError(OpenMeteoError):
    """Open-Meteo가 요청을 거부하거나 연결할 수 없을 때 발생한다."""


class OpenMeteoClient:
    """Open-Meteo 일별 예보 요청을 캡슐화한다."""

    def get_daily_forecasts(self, coordinates: Coordinates) -> list[dict[str, Any]]:
        """좌표의 오늘부터 15일 뒤까지 일별 예보를 반환한다."""

        query = urlencode(
            {
                "latitude": coordinates.latitude,
                "longitude": coordinates.longitude,
                "daily": ",".join(DAILY_VARIABLES),
                # Open-Meteo가 제공하는 최대 16일(오늘 포함)을 요청한다.
                "forecast_days": FORECAST_DAYS,
                # 좌표의 현지 날짜 기준으로 daily.time을 반환한다.
                "timezone": "auto",
                "temperature_unit": "celsius",
            }
        )
        request = Request(
            f"{OPEN_METEO_FORECAST_URL}?{query}",
            method="GET",
            headers={"Accept": "application/json"},
        )
        try:
            with urlopen(request, timeout=10) as response:
                content = response.read()
        except HTTPError as error:
            raise OpenMeteoRequestError(
                f"Open-Meteo API 요청이 거부되었습니다. (HTTP {error.code})"
            ) from error
        except URLError as error:
            raise OpenMeteoRequestError("Open-Meteo API에 연결하지 못했습니다.") from error
        except TimeoutError as error:
            raise OpenMeteoRequestError("Open-Meteo API 요청 시간이 초과되었습니다.") from error

        try:
            payload = json.loads(content.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise OpenMeteoRequestError(
                "Open-Meteo가 올바른 JSON 응답을 반환하지 않았습니다."
            ) from error

        daily = payload.get("daily") if isinstance(payload, dict) else None
        dates = daily.get("time") if isinstance(daily, dict) else None
        if not isinstance(dates, list):
            raise OpenMeteoRequestError("Open-Meteo 예보 형식이 올바르지 않습니다.")

        weather_codes = daily.get("weather_code")
        minimums = daily.get("temperature_2m_min")
        maximums = daily.get("temperature_2m_max")
        precipitation = daily.get("precipitation_probability_max")
        if not all(
            isinstance(values, list)
            for values in (weather_codes, minimums, maximums, precipitation)
        ):
            raise OpenMeteoRequestError("Open-Meteo 일별 예보 형식이 올바르지 않습니다.")

        forecasts: list[dict[str, Any]] = []
        for index, forecast_date in enumerate(dates):
            try:
                date_value = str(forecast_date)
                min_celsius = float(minimums[index])
                max_celsius = float(maximums[index])
                weather_code = int(float(weather_codes[index]))
                precipitation_percent = round(float(precipitation[index]))
            except (IndexError, TypeError, ValueError):
                continue
            forecasts.append(
                {
                    "date": date_value,
                    "label": WMO_WEATHER_LABELS.get(weather_code, "날씨 정보"),
                    "min_celsius": min_celsius,
                    "max_celsius": max_celsius,
                    "precipitation_percent": max(0, min(100, precipitation_percent)),
                }
            )

        if not forecasts:
            raise OpenMeteoRequestError("Open-Meteo 예보에 사용할 날씨 정보가 없습니다.")
        return forecasts
