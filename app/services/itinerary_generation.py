"""Google Places로 검증할 여행 일정 후보를 Gemini에게서 생성한다.

Gemini는 실제 저장할 장소명이나 내부 ID를 결정하지 않는다. DAY 번호, 시간,
장소 검색어만 반환하고, 라우터가 Google Places 검색 결과를 실제 일정 항목으로
바꾼다. 따라서 "공항 도착 및 입국 심사" 같은 일반 문구가 아니라 검증 가능한
실제 장소만 ``itinerary_items``에 저장할 수 있다.
"""

from __future__ import annotations

import json
import logging
import os
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone, tzinfo
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from google import genai
from google.genai import types
from pydantic import ValidationError

from app.schemas import ItineraryItemCreate


LOGGER = logging.getLogger(__name__)
_CLOCK_PATTERN = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")
_DEFAULT_TIMEZONE = "Asia/Seoul"
_MAX_PROMPT_CHARS = 2_000
_MAX_PLACE_QUERY_CHARS = 200
_FIRST_DAY_ARRIVAL_TIME = time(9, 0)
_FIRST_DAY_FIRST_PLACE_TIME = time(12, 0)
_REGULAR_DAY_FIRST_PLACE_TIME = time(9, 0)
_AI_PLACE_ITEM_TYPES = {"place", "cafe", "restaurant"}
_FALLBACK_UTC_OFFSETS = {
    "Asia/Seoul": 9,
    "Asia/Tokyo": 9,
    "Europe/Paris": 1,
    "America/New_York": -5,
}


@dataclass(frozen=True)
class _DayContext:
    """AI 초안을 저장된 DAY 하나에 연결할 때 쓰는 신뢰 가능한 일정 정보이다."""

    id: str
    day_number: int
    travel_date: date
    existing_items: tuple[dict[str, str], ...]


def generate_daily_itinerary_drafts(
    trip: Mapping[str, Any],
    days: Sequence[Mapping[str, Any]],
    user_request: str = "",
) -> list[dict[str, Any]]:
    """모든 DAY의 Google Places 검색용 일정 후보를 엄격히 검증해 반환한다.

    ``trip``에는 일반 대시보드 필드(특히 ``destination``, 필요하면 ``timezone``)가
    있어야 한다. ``days``의 각 항목에는 ``trip_days``가 반환한 ``id``,
    ``day_number``, ``travel_date``가 필요하다. 신뢰하는 대시보드 값의 형식이
    잘못되면 ``ValueError``를 발생시킨다. Gemini가 실패하거나 형식이 잘못되면
    대체 일정을 만들지 않고 예외를 전달한다. 새 여행을 만들기 전에 호출하는
    라우터가 이 예외를 처리하므로 불완전한 여행이 저장되지 않는다.
    """

    timezone = _resolve_timezone(trip.get("timezone"))
    day_contexts = _normalise_days(days)
    if not day_contexts:
        return []

    try:
        generated = _generate_gemini_json(trip, day_contexts, user_request)
        return _parse_and_validate_generated_items(generated, day_contexts, timezone)
    except Exception as error:
        # 제공자 원본 오류에는 요청 메타데이터가 들어갈 수 있으므로 서버 로그에는
        # 오류 종류만 남기고, 호출자에는 안전한 생성 실패 메시지만 전달한다.
        LOGGER.warning("AI 일정 후보 생성 실패 (%s).", type(error).__name__)
        raise ValueError("AI 일정 초안을 생성하지 못했습니다. 다시 시도해 주세요.") from error


def _resolve_timezone(value: Any) -> tzinfo:
    """여행 시간대를 사용하고 OS tzdata가 없으면 안전한 고정 오프셋을 사용한다.

    ``tzdata``는 프로젝트 의존성이므로, 일반 설치 환경에서는 완전한 IANA 시간대와
    일광 절약 시간 규칙을 사용한다. 작은 대체 처리는 의존성 동기화가 끝나기 전에도
    로컬 Windows 실습 환경을 사용할 수 있게 하며, 이 앱의 기본 여행지인 서울과
    도쿄에서는 정확하다.
    """

    timezone_name = str(value or _DEFAULT_TIMEZONE).strip() or _DEFAULT_TIMEZONE
    try:
        return ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError:
        offset_hours = _FALLBACK_UTC_OFFSETS.get(
            timezone_name, _FALLBACK_UTC_OFFSETS[_DEFAULT_TIMEZONE]
        )
        return timezone(timedelta(hours=offset_hours), name=timezone_name)


def _normalise_days(days: Sequence[Mapping[str, Any]]) -> list[_DayContext]:
    """AI 응답이 참조하기 전에 대시보드 DAY 정보를 검증한다."""

    contexts: list[_DayContext] = []
    seen_numbers: set[int] = set()
    for raw_day in days:
        day_id = str(raw_day.get("id") or "").strip()
        if not day_id:
            raise ValueError("일정 초안을 만들 DAY ID가 없습니다.")
        try:
            day_number = int(raw_day["day_number"])
            travel_date = date.fromisoformat(str(raw_day["travel_date"]))
        except (KeyError, TypeError, ValueError) as error:
            raise ValueError("일정 초안을 만들 DAY 날짜 정보가 올바르지 않습니다.") from error
        if day_number < 1 or day_number in seen_numbers:
            raise ValueError("DAY 번호는 중복되지 않는 1 이상의 값이어야 합니다.")
        seen_numbers.add(day_number)

        existing_items: list[dict[str, str]] = []
        for item in raw_day.get("items") or []:
            if not isinstance(item, Mapping):
                continue
            title = str(item.get("title") or "").strip()
            start_at = str(item.get("start_at") or "").strip()
            if title:
                existing_items.append({"title": title, "start_at": start_at})

        contexts.append(
            _DayContext(
                id=day_id,
                day_number=day_number,
                travel_date=travel_date,
                existing_items=tuple(existing_items),
            )
        )
    return sorted(contexts, key=lambda day: day.day_number)


def _generate_gemini_json(
    trip: Mapping[str, Any],
    days: Sequence[_DayContext],
    user_request: str,
) -> dict[str, Any]:
    """Gemini에 DAY 번호 기반의 신뢰하지 않는 일정 제안만 요청한다."""

    api_key = os.getenv("GEMINI_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY 값이 없습니다.")

    model = os.getenv("GEMINI_MODEL", "").strip() or "gemini-3.5-flash-lite"
    client = genai.Client(api_key=api_key)
    response = client.models.generate_content(
        model=model,
        contents=_generation_request(trip, days, user_request),
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            temperature=0.25,
            system_instruction=_generation_system_prompt(trip, days),
        ),
    )
    response_text = (getattr(response, "text", None) or "").strip()
    if not response_text:
        raise ValueError("Gemini가 빈 일정 JSON을 반환했습니다.")
    return _decode_json_object(response_text)


def _generation_system_prompt(
    trip: Mapping[str, Any], days: Sequence[_DayContext]
) -> str:
    """Google Places 검색 후보만 반환하도록 Gemini의 역할을 제한한다."""

    destination = str(trip.get("destination") or "여행지").strip() or "여행지"
    timezone = str(trip.get("timezone") or _DEFAULT_TIMEZONE).strip() or _DEFAULT_TIMEZONE
    day_lines = []
    for day in days:
        confirmed = ", ".join(
            f"{item['title']} ({item['start_at'] or '시간 미정'})"
            for item in day.existing_items
        ) or "없음"
        day_lines.append(
            f"DAY {day.day_number} / {day.travel_date.isoformat()} / 확정 일정: {confirmed}"
        )

    return f"""
당신은 TripMate의 여행 일정 초안 생성기입니다.
여행지: {destination}
시간대: {timezone}
대상 DAY:
{chr(10).join(day_lines)}

사용자에게 보여 줄 '초안'만 만드세요. 실제 영업시간, 예약 가능 여부,
교통 소요시간, 존재하지 않는 장소를 사실처럼 단정하지 마세요. 확정 일정과
시간이 겹치지 않도록 하며, 날짜를 넘기는 일정은 만들지 마세요.

첫 DAY는 여행지 현지 시각 { _FIRST_DAY_ARRIVAL_TIME.strftime('%H:%M') } 공항 도착을
가정합니다. 공항 도착, 입국 심사, 수하물 수령, 이동, 호텔 체크인, 휴식처럼
장소가 아닌 과정은 items에 넣지 마세요. 첫 DAY의 첫 실제 장소는 공항에서 시내로
이동하고 점심을 먹을 여유를 고려해 { _FIRST_DAY_FIRST_PLACE_TIME.strftime('%H:%M') } 이후에만
시작해야 합니다. DAY 2부터는 { _REGULAR_DAY_FIRST_PLACE_TIME.strftime('%H:%M') } 이전에
시작하는 일정이 없어야 합니다.

모든 item은 Google Places 텍스트 검색으로 실제 장소를 찾기 위한 후보여야 합니다.
`place_query`에는 장소 종류와 지역을 포함한 구체적인 검색어를 넣으세요. 예를 들어
"오사카 난바 오코노미야키", "오사카 우메다 스페셜티 커피", "오사카 도톤보리 관광지"
처럼 작성합니다. 일반 문구인 "점심 식사", "자유 시간", "공항 도착", "호텔 체크인",
"역으로 이동"은 place_query로 사용할 수 없습니다. 시스템이 Google의 실제 장소 이름으로
일정 제목을 바꾸므로 `title` 필드는 반환하지 마세요.

응답은 마크다운이나 설명 없이 아래 JSON 객체 하나여야 합니다. 모든 대상 DAY를
한 번씩 포함하고, 하루마다 1~4개의 items를 만드세요.

{{
  "days": [
    {{
      "day_number": 1,
      "items": [
        {{
          "item_type": "restaurant",
          "place_query": "오사카 난바 오코노미야키",
          "start_time": "12:00",
          "end_time": "13:30",
          "estimated_stay_minutes": 90,
          "travel_mode": "walk",
          "notes": "사용자가 검토할 짧은 이유"
        }}
      ]
    }}
  ]
}}

item_type은 place, cafe, restaurant 중 하나이고 travel_mode는 walk, transit, drive,
bicycle, flight 중 하나만 사용하세요.
""".strip()


def _generation_request(
    trip: Mapping[str, Any], days: Sequence[_DayContext], user_request: str
) -> str:
    """선택적인 사용자 선호를 시스템 JSON 규약과 분리한다."""

    title = str(trip.get("title") or "이 여행").strip() or "이 여행"
    request = str(user_request or "").strip()[:_MAX_PROMPT_CHARS]
    return (
        f"여행 제목: {title}\n"
        f"사용자 요청: {request or '특별 요청 없음. 균형 잡힌 가벼운 초안을 만들어 주세요.'}\n"
        f"생성할 DAY 번호: {', '.join(str(day.day_number) for day in days)}"
    )


def _decode_json_object(response_text: str) -> dict[str, Any]:
    """주변의 Markdown 코드 펜스를 허용하면서 Gemini JSON을 해석한다."""

    candidate = response_text.strip()
    if candidate.startswith("```"):
        candidate = re.sub(r"^```(?:json)?\s*", "", candidate, count=1, flags=re.I)
        candidate = re.sub(r"\s*```$", "", candidate, count=1)
    try:
        decoded = json.loads(candidate)
    except json.JSONDecodeError:
        # 일부 제공자는 JSON 응답 MIME 타입인데도 문장 하나를 앞에 붙인다.
        # 아래의 모든 필드는 일정 항목 값이 되기 전에 엄격히 검증하므로, 바깥
        # 객체를 추출하는 방식도 안전하다.
        start, end = candidate.find("{"), candidate.rfind("}")
        if start < 0 or end <= start:
            raise
        decoded = json.loads(candidate[start : end + 1])
    if not isinstance(decoded, dict):
        raise ValueError("Gemini 일정 응답은 JSON 객체여야 합니다.")
    return decoded


def _parse_and_validate_generated_items(
    generated: Mapping[str, Any],
    days: Sequence[_DayContext],
    timezone: tzinfo,
) -> list[dict[str, Any]]:
    """신뢰하지 않는 모델 필드를 신뢰 가능한 DAY ID에 연결하고 모든 항목을 검증한다."""

    raw_days = generated.get("days")
    if not isinstance(raw_days, list):
        raise ValueError("Gemini 일정 JSON에 days 배열이 없습니다.")

    contexts_by_number = {day.day_number: day for day in days}
    seen_day_numbers: set[int] = set()
    payloads: list[dict[str, Any]] = []
    for raw_day in raw_days:
        if not isinstance(raw_day, Mapping):
            raise ValueError("Gemini 일정 JSON의 DAY 형식이 올바르지 않습니다.")
        try:
            day_number = int(raw_day["day_number"])
        except (KeyError, TypeError, ValueError) as error:
            raise ValueError("Gemini 일정 JSON의 DAY 번호가 올바르지 않습니다.") from error
        if day_number not in contexts_by_number or day_number in seen_day_numbers:
            raise ValueError("Gemini가 알 수 없거나 중복된 DAY 번호를 반환했습니다.")
        seen_day_numbers.add(day_number)

        raw_items = raw_day.get("items")
        if not isinstance(raw_items, list) or not 1 <= len(raw_items) <= 4:
            raise ValueError("각 DAY에는 1~4개의 일정 항목이 필요합니다.")
        context = contexts_by_number[day_number]
        for raw_item in raw_items:
            if not isinstance(raw_item, Mapping):
                raise ValueError("Gemini 일정 항목 형식이 올바르지 않습니다.")
            payloads.append(_payload_from_model_item(context, raw_item, timezone))

    if seen_day_numbers != set(contexts_by_number):
        raise ValueError("Gemini가 일부 DAY의 일정 초안을 누락했습니다.")

    ordered_payloads = sorted(payloads, key=lambda item: (item["start_at"], item["title"]))
    latest_end_by_day: dict[str, datetime] = {}
    for payload in ordered_payloads:
        day_id = str(payload["trip_day_id"])
        start_at = datetime.fromisoformat(str(payload["start_at"]))
        end_at = datetime.fromisoformat(str(payload["end_at"]))
        latest_end = latest_end_by_day.get(day_id)
        if latest_end is not None and start_at < latest_end:
            raise ValueError("같은 DAY의 AI 일정 시간이 서로 겹칩니다.")
        latest_end_by_day[day_id] = end_at
    return ordered_payloads


def _payload_from_model_item(
    day: _DayContext,
    raw_item: Mapping[str, Any],
    timezone: tzinfo,
) -> dict[str, Any]:
    """Gemini 항목 하나를 ``ItineraryItemCreate`` 검증을 통과한 값으로 바꾼다."""

    start_clock = _parse_clock(raw_item.get("start_time"))
    end_clock = _parse_clock(raw_item.get("end_time"))
    earliest_start = (
        _FIRST_DAY_FIRST_PLACE_TIME
        if day.day_number == 1
        else _REGULAR_DAY_FIRST_PLACE_TIME
    )
    if start_clock < earliest_start:
        raise ValueError(
            f"DAY {day.day_number} 일정은 {earliest_start.strftime('%H:%M')} 이후에 시작해야 합니다."
        )
    start_at = datetime.combine(day.travel_date, start_clock, tzinfo=timezone)
    end_at = datetime.combine(day.travel_date, end_clock, tzinfo=timezone)
    if end_at <= start_at:
        raise ValueError("일정 종료 시각은 시작 시각보다 늦어야 합니다.")

    item_type = str(raw_item.get("item_type") or "").strip()
    if item_type not in _AI_PLACE_ITEM_TYPES:
        raise ValueError("AI 일정 항목 종류는 place, cafe, restaurant만 사용할 수 있습니다.")
    place_query = _place_query(raw_item.get("place_query"))
    estimated_stay_minutes = _positive_int(raw_item.get("estimated_stay_minutes"))
    if estimated_stay_minutes is None:
        estimated_stay_minutes = int((end_at - start_at).total_seconds() // 60)

    payload = _validated_payload(
        {
            "trip_day_id": day.id,
            "item_type": item_type,
            "source": "ai_recommendation",
            # 실제 Google 장소 이름은 라우터가 검색 결과에서 덮어쓴다. 여기서는
            # 기존 일정 스키마 검증을 위해 검색어를 임시 제목으로만 사용한다.
            "title": place_query[:150],
            "start_at": start_at,
            "end_at": end_at,
            "estimated_stay_minutes": estimated_stay_minutes,
            "is_fixed": False,
            "travel_mode": raw_item.get("travel_mode"),
            "notes": str(raw_item.get("notes") or "AI 일정 초안").strip(),
        }
    )
    # Pydantic 모델에 없는 내부 값이다. DB에 저장하기 전에 라우터가 반드시 꺼내
    # Google Places 검색에 사용하므로, 신뢰하지 않는 원본 모델 값이 DB로 가지 않는다.
    payload["_place_query"] = place_query
    return payload


def _parse_clock(value: Any) -> time:
    """모델이 반환한 값 중 모호하지 않은 ``HH:MM`` 시각만 허용한다."""

    clock_text = str(value or "").strip()
    if not _CLOCK_PATTERN.fullmatch(clock_text):
        raise ValueError("일정 시각은 HH:MM 형식이어야 합니다.")
    return time.fromisoformat(clock_text)


def _place_query(value: Any) -> str:
    """Google Places 검색에 쓸 짧고 구체적인 AI 장소 검색어를 검증한다."""

    query = str(value or "").strip()
    if not 2 <= len(query) <= _MAX_PLACE_QUERY_CHARS:
        raise ValueError("AI 장소 검색어는 2~200자여야 합니다.")

    lowered = query.casefold()
    disallowed_phrases = (
        "공항 도착",
        "입국 심사",
        "수하물",
        "호텔 체크인",
        "자유 시간",
        "자유 일정",
        "역으로 이동",
        "공항 이동",
        "airport arrival",
        "immigration",
        "hotel check-in",
        "free time",
    )
    if any(phrase in lowered for phrase in disallowed_phrases):
        raise ValueError("AI 일정에는 실제 장소 검색어만 사용할 수 있습니다.")
    return query


def _positive_int(value: Any) -> int | None:
    """안전한 모델 체류 시간을 반환하고, 없으면 시각으로 계산한 값을 사용한다."""

    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError("체류 시간은 숫자여야 합니다.")
    try:
        minutes = int(value)
    except (TypeError, ValueError) as error:
        raise ValueError("체류 시간은 숫자여야 합니다.") from error
    if not 1 <= minutes <= 720:
        raise ValueError("체류 시간은 1~720분이어야 합니다.")
    return minutes


def _validated_payload(values: Mapping[str, Any]) -> dict[str, Any]:
    """기존 일정 생성 API와 같은 Pydantic 규칙을 적용한다."""

    try:
        item = ItineraryItemCreate.model_validate(dict(values))
    except ValidationError as error:
        raise ValueError("Gemini 일정 항목이 기존 일정 형식에 맞지 않습니다.") from error
    return item.model_dump(mode="json", exclude_none=True)
