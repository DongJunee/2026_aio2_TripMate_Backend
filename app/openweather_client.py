"""백엔드에서만 사용하는 OpenWeather 5일 예보 클라이언트이다.

OpenWeather의 5일/3시간 예보 응답을 여행 화면에서 쓰기 좋은 일별 최저·최고 기온,
강수 확률, 날씨 설명으로 묶어 반환한다. API 키가 URL에 포함되므로 이 모듈은
FastAPI 백엔드에서만 호출하고 Streamlit으로는 가공된 날씨 정보만 전달한다.
"""

import json
import os
from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from app.google_maps_client import Coordinates


OPENWEATHER_FORECAST_URL = "https://api.openweathermap.org/data/2.5/forecast"


class OpenWeatherError(RuntimeError):
    """완료할 수 없는 OpenWeather 요청의 기본 오류이다."""


class OpenWeatherUnavailableError(OpenWeatherError):
    """OpenWeather API 키가 설정되지 않은 경우 발생한다."""


class OpenWeatherRequestError(OpenWeatherError):
    """OpenWeather가 요청을 거부하거나 연결할 수 없을 때 발생한다."""


class OpenWeatherClient:
    """OpenWeather 5일/3시간 예보 요청을 캡슐화한다."""

    def __init__(self, api_key: str):
        """비어 있지 않은 OpenWeather API 키를 보관한다."""

        if not api_key.strip():
            raise OpenWeatherUnavailableError("OPENWEATHER_API_KEY를 설정하세요.")
        self._api_key = api_key.strip()

    @classmethod
    def from_environment(cls) -> "OpenWeatherClient":
        """환경 변수에서 클라이언트를 만들며 이전 변수명도 함께 지원한다."""

        # 사용 중인 프로젝트의 기존 변수명(OPENWEATHERMAP_API_KEY)도 읽어,
        # .env를 당장 다시 작성하지 않아도 날씨 기능이 동작하게 한다.
        api_key = os.getenv("OPENWEATHER_API_KEY") or os.getenv("OPENWEATHERMAP_API_KEY")
        if not api_key:
            raise OpenWeatherUnavailableError("OPENWEATHER_API_KEY를 설정하세요.")
        return cls(api_key)

    def get_daily_forecasts(self, coordinates: Coordinates) -> list[dict[str, Any]]:
        """좌표의 5일/3시간 예보를 화면용 일별 요약 목록으로 반환한다."""

        query = urlencode(
            {
                "lat": coordinates.latitude,
                "lon": coordinates.longitude,
                "appid": self._api_key,
                "units": "metric",
                "lang": "kr",
            }
        )
        request = Request(
            f"{OPENWEATHER_FORECAST_URL}?{query}",
            method="GET",
            headers={"Accept": "application/json"},
        )
        try:
            with urlopen(request, timeout=10) as response:
                content = response.read()
        except HTTPError as error:
            raise OpenWeatherRequestError(
                f"OpenWeather API 요청이 거부되었습니다. (HTTP {error.code})"
            ) from error
        except URLError as error:
            raise OpenWeatherRequestError("OpenWeather API에 연결하지 못했습니다.") from error
        except TimeoutError as error:
            raise OpenWeatherRequestError("OpenWeather API 요청 시간이 초과되었습니다.") from error

        try:
            payload = json.loads(content.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise OpenWeatherRequestError("OpenWeather가 올바른 JSON 응답을 반환하지 않았습니다.") from error

        entries = payload.get("list") if isinstance(payload, dict) else None
        city = payload.get("city") if isinstance(payload, dict) else None
        if not isinstance(entries, list) or not isinstance(city, dict):
            raise OpenWeatherRequestError("OpenWeather 예보 형식이 올바르지 않습니다.")
        try:
            city_timezone = int(city.get("timezone") or 0)
        except (TypeError, ValueError):
            city_timezone = 0
        local_timezone = timezone(timedelta(seconds=city_timezone))

        grouped: dict[str, dict[str, list[Any] | float]] = {}
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            try:
                forecast_at = datetime.fromtimestamp(int(entry["dt"]), tz=local_timezone)
            except (KeyError, TypeError, ValueError, OSError):
                continue
            main = entry.get("main") if isinstance(entry.get("main"), dict) else {}
            weather = entry.get("weather") if isinstance(entry.get("weather"), list) else []
            bucket = grouped.setdefault(
                forecast_at.date().isoformat(),
                {"temperatures": [], "precipitation": 0.0, "descriptions": []},
            )
            for value in (main.get("temp_min"), main.get("temp_max"), main.get("temp")):
                try:
                    bucket["temperatures"].append(float(value))  # type: ignore[index,union-attr]
                except (TypeError, ValueError):
                    continue
            try:
                bucket["precipitation"] = max(  # type: ignore[index]
                    float(bucket["precipitation"]), float(entry.get("pop") or 0)  # type: ignore[index]
                )
            except (TypeError, ValueError):
                pass
            if weather and isinstance(weather[0], dict):
                description = str(weather[0].get("description") or "").strip()
                if description:
                    bucket["descriptions"].append(description)  # type: ignore[index,union-attr]

        forecasts: list[dict[str, Any]] = []
        for forecast_date in sorted(grouped):
            bucket = grouped[forecast_date]
            temperatures = bucket["temperatures"]
            if not temperatures:
                continue
            descriptions = bucket["descriptions"]
            label = Counter(descriptions).most_common(1)[0][0] if descriptions else "날씨 정보"
            forecasts.append(
                {
                    "date": forecast_date,
                    "label": label,
                    "min_celsius": min(temperatures),
                    "max_celsius": max(temperatures),
                    "precipitation_percent": round(float(bucket["precipitation"]) * 100),
                }
            )
        if not forecasts:
            raise OpenWeatherRequestError("OpenWeather 예보에 사용할 날씨 정보가 없습니다.")
        return forecasts
