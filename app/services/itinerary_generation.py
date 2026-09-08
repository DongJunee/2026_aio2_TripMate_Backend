"""Google Places로 검증할 여행 일정 후보를 Gemini에게서 생성한다.

서버가 여행 강도에 맞는 식사·활동·휴식 순서와 현지 시각을 정한다. Gemini는
식사·활동 칸의 장소 검색어만 추천하고 라우터가 실제 Google 장소를 연결한다.
숙소와 예약 시간이 알려지지 않은 체크인·휴식은 장소를 꾸며내지 않고 안내 항목으로 둔다.
"""

from __future__ import annotations

import json
import logging
import os
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, tzinfo
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from google import genai
from google.genai import types
from pydantic import ValidationError

from app.schemas import ItineraryItemCreate
from app.services.travel_preferences import (
    DailyScheduleSlot,
    daily_schedule_slots,
    is_departure_day,
    travel_preferences_text,
)


LOGGER = logging.getLogger(__name__)
_MAX_PROMPT_CHARS = 2_000
_MAX_PLACE_QUERY_CHARS = 200
_MAX_NOTES_CHARS = 1_000


@dataclass(frozen=True)
class GeneratedItinerary:
    """현지 시간으로 검증된 일정과 trips에 함께 저장할 IANA 시간대이다."""

    timezone: str
    items: list[dict[str, Any]]


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
) -> GeneratedItinerary:
    """모든 DAY의 Google Places 검색용 일정 후보를 엄격히 검증해 반환한다.

    ``trip``에는 특히 ``destination``이 있어야 한다. 시간대를 생략하면 같은 Gemini
    응답에서 여행지의 IANA 시간대도 받아 검증한다. ``days``의 각 항목에는 ``id``,
    ``day_number``, ``travel_date``가 필요하다. 신뢰하는 대시보드 값의 형식이
    잘못되면 ``ValueError``를 발생시킨다. Gemini가 실패하거나 형식이 잘못되면
    대체 일정을 만들지 않고 예외를 전달한다. 새 여행을 만들기 전에 호출하는
    라우터가 이 예외를 처리하므로 불완전한 여행이 저장되지 않는다.
    """

    day_contexts = _normalise_days(days)
    if not day_contexts:
        raise ValueError("일정 초안을 만들 DAY가 없습니다.")

    try:
        generated = _generate_gemini_json(trip, day_contexts, user_request)
        requested_timezone = str(trip.get("timezone") or "").strip()
        generated_timezone = str(generated.get("timezone") or "").strip()
        if requested_timezone and generated_timezone and requested_timezone != generated_timezone:
            raise ValueError("AI가 요청한 여행 시간대와 다른 시간대를 반환했습니다.")
        timezone = _resolve_timezone(requested_timezone or generated_timezone)
        items = _parse_and_validate_generated_items(
            generated, day_contexts, timezone, trip.get("travel_intensity", 3), trip.get("end_date")
        )
        return GeneratedItinerary(timezone=timezone.key, items=items)
    except Exception as error:
        # 제공자 원본 오류에는 요청 메타데이터가 들어갈 수 있으므로 서버 로그에는
        # 오류 종류만 남기고, 호출자에는 안전한 생성 실패 메시지만 전달한다.
        LOGGER.warning("AI 일정 후보 생성 실패 (%s).", type(error).__name__)
        raise ValueError("AI 일정 초안을 생성하지 못했습니다. 다시 시도해 주세요.") from error


def _resolve_timezone(value: Any) -> ZoneInfo:
    """실제 IANA 시간대만 허용하고 여행 날짜의 서머타임까지 적용한다."""

    timezone_name = str(value or "").strip()
    if not timezone_name:
        raise ValueError("AI가 여행지 시간대를 반환하지 않았습니다.")
    try:
        return ZoneInfo(timezone_name)
    except (ZoneInfoNotFoundError, ValueError) as error:
        # 임의의 한국 시각이나 고정 오프셋으로 대체하면 다른 여행지의 시간이 틀린다.
        # Windows를 포함한 실행 환경에는 프로젝트 의존성인 tzdata가 필요하다.
        raise ValueError("여행지 시간대가 유효하지 않거나 tzdata가 설치되지 않았습니다.") from error


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
    """서버가 정한 칸마다 Google Places 검색 후보 하나만 반환하도록 요청한다."""

    destination = str(trip.get("destination") or "여행지").strip() or "여행지"
#LSW 0908 
    # 사용자가 직접 고른 장소는 모델이 추천한 장소보다 우선한다.
    # 이 목록이 비면 프롬프트에 빈 줄이 들어가지 않도록 문자열 자체를 비운다 —
    # 조건 없는 안내문은 모델이 없는 제약을 지어내는 원인이 된다.
    # trip.get 으로 읽으므로, 이 키가 없는 다른 호출 경로
    # (DB 에서 읽은 여행 행 등)는 그대로 None 을 받아 영향이 없다.
    must_visit = [
        text for name in trip.get("must_visit") or []
        if (text := str(name).strip())
    ]
    must_visit_instruction = (
        "\n사용자가 직접 고른 '가고 싶은 장소': " + ", ".join(must_visit) + "\n"
        "이 장소들을 어울리는 slot 의 place_query 로 먼저 배치하세요. "
        "여행 도시 밖이거나 slot 종류(식당/활동)와 맞지 않으면 넣지 말고 다른 장소로 채우세요. "
        "이 목록 때문에 칸을 추가하거나 시간을 바꾸지 마세요."
        if must_visit else ""
    )
    timezone = str(trip.get("timezone") or "").strip()
    timezone_instruction = (
        f"지정된 IANA 시간대: {timezone}. 응답 timezone에도 이 값을 그대로 사용하세요."
        if timezone
        else "여행지 도시의 IANA 시간대를 판단해 응답 timezone에 넣으세요. "
             "예: 오사카는 Asia/Tokyo, 호놀룰루는 Pacific/Honolulu, 파리는 Europe/Paris. "
             "사용자나 서버의 시간대, UTC 오프셋 숫자는 사용하지 마세요."
    )
    day_lines = []
    slot_lines = []
    example_days = []
    for day in days:
        confirmed = ", ".join(
            f"{item['title']} ({item['start_at'] or '시간 미정'})"
            for item in day.existing_items
        ) or "없음"
        day_lines.append(
            f"DAY {day.day_number} / {day.travel_date.isoformat()} / 확정 일정: {confirmed}"
        )
        departure_day = is_departure_day(day.travel_date, trip.get("end_date"))
        slots = daily_schedule_slots(trip.get("travel_intensity", 3), departure_day=departure_day)
        slot_lines.append(
            f"DAY {day.day_number} ({'출국일: 관광·식사는 13:00까지' if departure_day else '일반 일정'}):"
        )
        slot_lines.extend(
            f"  {slot.slot_id}: {slot.start_time:%H:%M}~{slot.end_time:%H:%M} "
            f"{'점심 식당' if slot.slot_id == 'lunch' else '저녁 식당' if slot.slot_id == 'dinner' else '관광·활동 장소'}"
            for slot in slots if slot.needs_place
        )
        example_days.append({
            "day_number": day.day_number,
            "items": [
                {"slot_id": slot.slot_id, "place_query": "여행지 내 구체적인 실제 장소 검색어", "notes": "추천 이유"}
                for slot in slots if slot.needs_place
            ],
        })
    example = json.dumps(
        {"timezone": timezone or "Asia/Tokyo", "days": example_days},
        ensure_ascii=False,
        indent=2,
    )

    return f"""
당신은 TripMate의 여행 일정 초안 생성기입니다.
여행지: {destination}{must_visit_instruction}
{timezone_instruction}
대상 DAY:
{chr(10).join(day_lines)}
여행 조건:
{travel_preferences_text(trip)}

사용자에게 보여 줄 '초안'만 만드세요. 실제 영업시간, 예약 가능 여부,
교통 소요시간, 존재하지 않는 장소를 사실처럼 단정하지 마세요. 확정 일정과
중복된 장소는 피하고, 가까운 지역끼리 묶으세요. 하루의 이동·체류 부담과
소비 수준에 어울리는 장소를 고르되 확인하지 않은 접근성·키즈 시설·가격을 보장하지 마세요.
시니어 부부는 무조건 온천, 가족은 무조건 놀이공원처럼 단정하지 마세요.
선택한 여행 도시 안에 있는 장소만 추천하세요. 근교나 다른 도시 방문은 금지합니다.
예를 들어 오사카 여행에 교토·나라·고베 장소를 넣지 마세요. 여행 강도가 높아도
추천 범위를 다른 도시로 넓히지 마세요. 숙소 입력이 없어도 이 도시 제한은 유지됩니다.

서버가 모든 DAY의 일정을 여행지 현지 오전 09:00부터 구성합니다. 아래 시간은
현지 벽시계 기준이며 날짜별 서머타임은 서버가 처리합니다. 각 DAY에서 아래 slot_id를
정확히 한 번씩 채우세요. 칸을 추가·삭제하거나 시간을 정하지 마세요.
{chr(10).join(slot_lines)}
activity는 관광·문화·체험 등 식사와 구분되는 활동이고 lunch/dinner는 실제 식당입니다.
오전·야간 시각에 어울리는 장소를 추천하고, 야간에는 실내시설 영업을 임의로 가정하지 마세요.
출국일이 아닌 첫날 체크인(시간 미정), 오전 여유 시간, 호텔 휴식은 서버가 따로 추가합니다.
출국일은 여행의 실제 종료일({trip.get('end_date') or '미정'})이며, 생성 대상 DAY 중 마지막이라는 이유로 출국일이 되지 않습니다.
출국일은 현지 18시 출발을 가정하고 13시 이후 관광·식사·호텔 일정은 만들지 않습니다.
공항 이동 13~15시, 수속 준비 15~18시, 18시 출국 안내는 서버가 예비 시간으로 추가합니다.
이것은 실제 항공권·공항 또는 경로 이동시간 확인 결과가 아닙니다.
이 안내 항목과 공항 도착·입국 심사·수하물 수령·이동 과정을 items에 넣지 마세요.

모든 item은 Google Places 텍스트 검색으로 실제 장소를 찾기 위한 후보여야 합니다.
`place_query`에는 장소 종류와 지역을 포함한 구체적인 검색어를 넣으세요. 예를 들어
"오사카 난바 오코노미야키", "오사카 우메다 스페셜티 커피", "오사카 도톤보리 관광지"
처럼 작성합니다. 일반 문구인 "점심 식사", "자유 시간", "공항 도착", "호텔 체크인",
"역으로 이동"은 place_query로 사용할 수 없습니다. 시스템이 Google의 실제 장소 이름으로
일정 제목을 바꾸므로 `title` 필드는 반환하지 마세요.

응답은 마크다운이나 설명 없이 아래 JSON 객체 하나여야 합니다. 모든 대상 DAY를
한 번씩 포함하고, 하루마다 위에서 지정한 모든 slot_id의 items를 만드세요.
각 item은 slot_id, place_query, notes 세 필드만 사용하세요.

{example}

위 예시의 여행지와 timezone은 형식 설명용입니다. 실제 응답은 요청한 여행지에 맞추세요.
""".strip()


def _generation_request(
    trip: Mapping[str, Any], days: Sequence[_DayContext], user_request: str
) -> str:
    """선택적인 사용자 선호를 시스템 JSON 규약과 분리한다."""

    title = str(trip.get("title") or "이 여행").strip() or "이 여행"
    request = str(user_request or "").strip()[:_MAX_PROMPT_CHARS]
    return (
        f"여행 제목: {title}\n"
        f"사용자 요청: {request or '특별 요청 없음. 저장한 동행 구성·여행 강도·경비에 맞는 초안을 만들어 주세요.'}\n"
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
    intensity: int = 3,
    end_date: Any = None,
) -> list[dict[str, Any]]:
    """모든 DAY의 식사·활동 칸이 정확한지 확인한 뒤 서버 시각표에 연결한다."""

    raw_days = generated.get("days")
    if not isinstance(raw_days, list):
        raise ValueError("Gemini 일정 JSON에 days 배열이 없습니다.")

    contexts_by_number = {day.day_number: day for day in days}
    slots_by_day = {
        day.day_number: daily_schedule_slots(intensity, departure_day=is_departure_day(day.travel_date, end_date))
        for day in days
    }
    model_items_by_day: dict[int, dict[str, Mapping[str, Any]]] = {}
    for raw_day in raw_days:
        if not isinstance(raw_day, Mapping):
            raise ValueError("Gemini 일정 JSON의 DAY 형식이 올바르지 않습니다.")
        day_number = raw_day.get("day_number")
        if isinstance(day_number, bool) or not isinstance(day_number, int):
            raise ValueError("Gemini 일정 JSON의 DAY 번호가 올바르지 않습니다.")
        if day_number not in contexts_by_number or day_number in model_items_by_day:
            raise ValueError("Gemini가 알 수 없거나 중복된 DAY 번호를 반환했습니다.")

        expected_slot_ids = {slot.slot_id for slot in slots_by_day[day_number] if slot.needs_place}
        raw_items = raw_day.get("items")
        if not isinstance(raw_items, list) or len(raw_items) != len(expected_slot_ids):
            raise ValueError("AI가 여행 강도와 출국일에 맞는 식사·활동 개수를 반환하지 않았습니다.")
        items_by_slot: dict[str, Mapping[str, Any]] = {}
        for raw_item in raw_items:
            if not isinstance(raw_item, Mapping):
                raise ValueError("Gemini 일정 항목 형식이 올바르지 않습니다.")
            if set(raw_item) - {"slot_id", "place_query", "notes"}:
                raise ValueError("AI는 일정 시각·종류 대신 장소 검색어와 추천 이유만 반환해야 합니다.")
            slot_id = raw_item.get("slot_id")
            if not isinstance(slot_id, str) or slot_id not in expected_slot_ids or slot_id in items_by_slot:
                raise ValueError("AI가 알 수 없거나 중복된 일정 칸을 반환했습니다.")
            items_by_slot[slot_id] = raw_item
        if set(items_by_slot) != expected_slot_ids:
            raise ValueError("AI가 일부 식사·활동 칸을 누락했습니다.")
        model_items_by_day[day_number] = items_by_slot

    if set(model_items_by_day) != set(contexts_by_number):
        raise ValueError("Gemini가 일부 DAY의 일정 초안을 누락했습니다.")

    payloads: list[dict[str, Any]] = []
    # 모델의 배열 순서와 무관하게 DAY와 서버 칸 순서를 사용한다. 체크인은 시각이
    # 없으므로 start_at 문자열로 전체 정렬하지 않고 첫날 맨 앞에만 안내로 둔다.
    for day in sorted(days, key=lambda context: context.day_number):
        if day.day_number == 1 and not is_departure_day(day.travel_date, end_date):
            check_in = _validated_payload({
                "trip_day_id": day.id,
                "item_type": "hotel",
                "source": "ai_recommendation",
                "title": "체크인 · 숙소/시간 확인 필요",
                "notes": "예약한 숙소와 체크인 가능 시간을 확인한 뒤 시간을 정하세요. 오전 9시 체크인을 의미하지 않습니다.",
                "is_fixed": False,
            })
            check_in["_activity_only"] = True
            payloads.append(check_in)
        for slot in slots_by_day[day.day_number]:
            raw_item = model_items_by_day[day.day_number].get(slot.slot_id)
            payloads.append(_payload_from_slot(day, slot, raw_item, timezone))
    return payloads


def _payload_from_slot(
    day: _DayContext,
    slot: DailyScheduleSlot,
    raw_item: Mapping[str, Any] | None,
    timezone: tzinfo,
) -> dict[str, Any]:
    """서버 시각과 검증한 검색어로 장소 일정 또는 숙소 미정 안내를 만든다."""

    start_at = datetime.combine(day.travel_date, slot.start_time, tzinfo=timezone)
    end_at = datetime.combine(day.travel_date, slot.end_time, tzinfo=timezone)
    place_query = None
    title, notes = slot.title, slot.notes
    if slot.needs_place:
        if raw_item is None:
            raise ValueError("장소 일정 칸의 AI 추천이 없습니다.")
        place_query = _place_query(raw_item.get("place_query"))
        raw_notes = raw_item.get("notes", "AI 일정 초안")
        if not isinstance(raw_notes, str) or len(raw_notes) > _MAX_NOTES_CHARS:
            raise ValueError("AI 추천 이유는 1,000자 이하 문자열이어야 합니다.")
        title = place_query[:150]
        notes = raw_notes.strip() or "AI 일정 초안"

    payload = _validated_payload(
        {
            "trip_day_id": day.id,
            "item_type": slot.item_type,
            "source": "ai_recommendation",
            # 실제 Google 장소 이름은 라우터가 검색 결과에서 덮어쓴다. 여기서는
            # 기존 일정 스키마 검증을 위해 검색어를 임시 제목으로만 사용한다.
            "title": title,
            "start_at": start_at,
            "end_at": end_at,
            "estimated_stay_minutes": int((end_at - start_at).total_seconds() // 60),
            "is_fixed": False,
            "notes": notes,
        }
    )
    # 두 표시는 서버만 만드는 내부 값이며 라우터가 DB 저장 전에 제거한다.
    if place_query is not None:
        payload["_place_query"] = place_query
    else:
        payload["_activity_only"] = True
    return payload


def _place_query(value: Any) -> str:
    """Google Places 검색에 쓸 짧고 구체적인 AI 장소 검색어를 검증한다."""

    if not isinstance(value, str):
        raise ValueError("AI 장소 검색어는 문자열이어야 합니다.")
    query = value.strip()
    if not 2 <= len(query) <= _MAX_PLACE_QUERY_CHARS:
        raise ValueError("AI 장소 검색어는 2~200자여야 합니다.")

    lowered = query.casefold()
    disallowed_phrases = (
        "공항 도착",
        "입국 심사",
        "수하물",
        "호텔 체크인",
        "호텔 휴식",
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


def _validated_payload(values: Mapping[str, Any]) -> dict[str, Any]:
    """기존 일정 생성 API와 같은 Pydantic 규칙을 적용한다."""

    try:
        item = ItineraryItemCreate.model_validate(dict(values))
    except ValidationError as error:
        raise ValueError("Gemini 일정 항목이 기존 일정 형식에 맞지 않습니다.") from error
    return item.model_dump(mode="json", exclude_none=True)
