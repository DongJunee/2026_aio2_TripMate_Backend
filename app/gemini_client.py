"""Gemini는 문장 작성과 여행 안내에만 사용하며, 데이터베이스 권한 확인에는 쓰지 않는다."""

import json
import os
import re
from collections.abc import Iterator
from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from google import genai
from google.genai import types

from app.services.travel_preferences import travel_preferences_text


def _looks_like_schedule_swap_request(message: str) -> bool:
    """일정 두 칸의 위치 교환을 요청한 문장만 Gemini 해석 대상으로 고른다."""

    text = message.strip()
    if not text:
        return False
    has_swap_word = any(word in text for word in ("바꿔", "교환", "순서 변경", "순서 바꿔"))
    # 시간만 수정해 달라는 문장을 장소 교환으로 오해하면 안 된다.
    has_time_only_word = any(word in text for word in ("시간", "시각", "시부터", "시로"))
    return has_swap_word and not has_time_only_word


def _normalized_schedule_label(value: object) -> str:
    """일정 제목 비교에서 띄어쓰기·문장부호·대소문자 차이를 무시한다."""

    return "".join(character for character in str(value or "").casefold() if character.isalnum())


def _explicit_schedule_swap_ids(
    candidates: list[dict[str, object]],
    user_message: str,
    timezone_name: object,
) -> tuple[str, str] | None:
    """시간 또는 제목을 정확히 말한 교환 요청은 모델 호출 없이 안전하게 찾는다.

    예를 들어 '12시 일정의 난바 파크스와 14시 카레집을 바꿔줘'는 두 시간과
    제목을 모두 제공한다. 이 경우 Gemini가 JSON 형식을 조금 다르게 반환해도
    기능이 실패하지 않도록, 현재 일정 후보 안에서 유일하게 일치하는 두 항목만
    바로 선택한다. 하나라도 중복·불명확하면 None을 반환해 아래 Gemini 해석으로
    넘긴다.
    """

    normalized_message = _normalized_schedule_label(user_message)
    named_matches = [
        candidate
        for candidate in candidates
        if (title := _normalized_schedule_label(candidate.get("title")))
        and len(title) >= 3
        and title in normalized_message
    ]
    if len(named_matches) == 2 and len({str(item["day_number"]) for item in named_matches}) == 1:
        return str(named_matches[0]["id"]), str(named_matches[1]["id"])

    mentioned_hours = list(dict.fromkeys(re.findall(r"(?<!\d)([01]?\d|2[0-3])\s*시", user_message)))
    if len(mentioned_hours) < 2:
        return None
    try:
        trip_timezone = ZoneInfo(str(timezone_name or "Asia/Seoul"))
    except ZoneInfoNotFoundError:
        trip_timezone = ZoneInfo("Asia/Seoul")
    matched_by_hour: list[dict[str, object]] = []
    for hour_text in mentioned_hours[:2]:
        hour = int(hour_text)
        matches: list[dict[str, object]] = []
        for candidate in candidates:
            value = candidate.get("start_at")
            try:
                parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=trip_timezone)
                else:
                    parsed = parsed.astimezone(trip_timezone)
                start_hour = parsed.hour
            except (TypeError, ValueError):
                continue
            if start_hour == hour:
                matches.append(candidate)
        if len(matches) != 1:
            return None
        matched_by_hour.append(matches[0])
    if len({str(item["id"]) for item in matched_by_hour}) != 2:
        return None
    if len({str(item["day_number"]) for item in matched_by_hour}) != 1:
        return None
    return str(matched_by_hour[0]["id"]), str(matched_by_hour[1]["id"])


def resolve_itinerary_place_swap_request(
    trip: dict,
    days: list[dict],
    user_message: str,
) -> tuple[str, str] | None:
    """자연어에서 안전하게 확정할 수 있는 일정 장소 교환 대상 두 ID를 찾는다.

    Gemini는 후보 ID를 고르는 역할만 한다. 실제 일정 조회·같은 DAY 확인·저장과
    변경 로그 기록은 호출한 백엔드 라우터가 수행하므로 모델이 다른 사용자의 일정
    이나 존재하지 않는 장소를 바꿀 수 없다.
    """

    if not _looks_like_schedule_swap_request(user_message):
        return None

    candidates: list[dict[str, object]] = []
    for day in days:
        for item in day.get("items") or []:
            item_id = str(item.get("id") or "").strip()
            if not item_id:
                continue
            candidates.append(
                {
                    "id": item_id,
                    "day_number": day.get("day_number"),
                    "date": day.get("travel_date"),
                    "title": item.get("title") or "일정",
                    "item_type": item.get("item_type") or "place",
                    "start_at": item.get("start_at"),
                    "end_at": item.get("end_at"),
                }
            )
    if len(candidates) < 2:
        return None

    # 시간·장소명이 명확한 요청은 LLM 응답의 JSON 형식에 영향을 받지 않도록 먼저
    # 처리한다. 문장이 모호할 때만 아래 Gemini가 현재 일정의 ID를 골라 준다.
    if explicit_ids := _explicit_schedule_swap_ids(
        candidates, user_message, trip.get("timezone")
    ):
        return explicit_ids

    api_key = os.getenv("GEMINI_API_KEY", "").strip()
    if not api_key:
        # 일반 채팅도 같은 설정을 쓰므로, 여기서 별도 오류를 만들지 않고 기존
        # 스트리밍 답변의 설정 오류를 그대로 보여 준다.
        return None
    model = os.getenv("GEMINI_MODEL", "").strip() or "gemini-3.5-flash-lite"
    system_prompt = """
당신은 TripMate 서버의 일정 변경 의도 판별기입니다.
사용자 문장과 제공된 일정 후보만 보고, 사용자가 두 일정의 '장소'를 서로 바꾸라고
명확히 요청한 경우에만 JSON을 반환하세요. 시간 변경, 추천 요청, 애매한 지시 또는
후보에 없는 장소는 action을 none으로 반환합니다. 반드시 후보에 있는 id 두 개만
사용하고, 같은 DAY의 두 일정만 고르세요. 설명·Markdown 없이 JSON 객체만 반환하세요.

형식:
{"action":"swap_places","first_item_id":"UUID","second_item_id":"UUID"}
또는
{"action":"none"}
""".strip()
    prompt = (
        f"여행지: {trip.get('destination') or '미정'}\n"
        f"일정 후보: {json.dumps(candidates, ensure_ascii=False, default=str)}\n"
        f"사용자 요청: {user_message.strip()}"
    )
    try:
        client = genai.Client(api_key=api_key)
        response = client.models.generate_content(
            model=model,
            contents=prompt,
            config=types.GenerateContentConfig(
                system_instruction=system_prompt,
                response_mime_type="application/json",
            ),
        )
        parsed = json.loads(str(response.text or ""))
    except Exception:
        # 이 보조 기능의 제공자 오류가 일반 여행 채팅을 막으면 안 된다. 호출자는
        # None일 때 원래의 Gemini 스트리밍 응답을 계속 사용한다.
        return None
    if not isinstance(parsed, dict) or parsed.get("action") != "swap_places":
        return None
    first_item_id = str(parsed.get("first_item_id") or "").strip()
    second_item_id = str(parsed.get("second_item_id") or "").strip()
    known_ids = {str(candidate["id"]) for candidate in candidates}
    if (
        not first_item_id
        or not second_item_id
        or first_item_id == second_item_id
        or first_item_id not in known_ids
        or second_item_id not in known_ids
    ):
        return None
    return first_item_id, second_item_id


def _schedule_summary(days: list[dict], timezone_name: str = "Asia/Seoul") -> str:
    """여행지 현지 시각으로 확정 일정을 요약해 채팅도 같은 시계를 사용하게 한다."""
    trip_timezone = ZoneInfo(timezone_name)

    def local_start(item: dict) -> str:
        """숙소 체크인처럼 아직 시간이 없는 항목은 미정으로 남긴다."""
        value = item.get("start_at")
        if not value:
            return "시간 미정"
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=trip_timezone)
        return parsed.astimezone(trip_timezone).strftime("%Y-%m-%d %H:%M")

    lines: list[str] = []
    for day in days:
        items = day.get("items", [])
        item_text = ", ".join(
            f"{item.get('title')}({local_start(item)})"
            for item in items
        ) or "일정 없음"
        lines.append(
            f"DAY {day.get('day_number')} {day.get('travel_date')}: {item_text}"
        )
    return "\n".join(lines)[:5000]


def _travel_generation_inputs(
    trip: dict,
    days: list[dict],
    history: list[dict],
    user_message: str,
    mate_type: str = "assistant",
) -> tuple[str, str, str, list[dict]]:
    """일반 응답과 스트리밍 응답이 공통으로 쓰는 Gemini 입력을 만든다.

    두 방식의 프롬프트나 대화 범위가 달라지면 같은 질문에도 답변 품질이
    달라질 수 있다. 그래서 API 키, 모델, 시스템 프롬프트, 최근 대화 구성은
    이 함수 한 곳에서 만든다.
    """

    api_key = os.getenv("GEMINI_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY 값을 backend/.env에 입력하세요.")

    # .env에서 모델을 비워 두면 실습용 기본 모델을 사용한다.
    model = os.getenv("GEMINI_MODEL", "").strip() or "gemini-3.5-flash-lite"
    mate_guidance = {
        "assistant": "핵심 내용을 먼저 짧고 명확하게 전달하는 비서처럼 답하세요.",
        "guide": "여행 가이드처럼 배경 설명과 선택지를 함께 제안하되, 답변은 이해하기 쉽게 정리하세요.",
        "senior": "어르신도 읽기 쉽도록 쉬운 단어와 짧은 문장을 사용하고, 순서를 나누어 천천히 설명하세요.",
    }.get(
        str(mate_type or "assistant"),
        "핵심 내용을 먼저 짧고 명확하게 전달하는 비서처럼 답하세요.",
    )
    system_prompt = f"""
당신은 TripMate의 AI 여행 플래너입니다.
여행 제목: {trip.get('title')}
여행지: {trip.get('destination') or '미정'}
시간대: {trip.get('timezone') or 'Asia/Seoul'}
현재 Mate 방식:
{mate_guidance}

현재 저장된 여행 조건:
{travel_preferences_text(trip)}

현재 확정된 일정:
{_schedule_summary(days, trip.get('timezone') or 'Asia/Seoul')}

한국어로 친절하고 짧게 답하세요. 일정에 없는 사실, 실제 영업시간,
실시간 교통시간을 지어내지 마세요. 사용자가 카페·식당·명소처럼 장소 추천을
요청하면 화면에 표시되는 Google 장소 추천 카드에서 후보를 골라 일정에 추가할 수
있다고 안내하세요. 카드에 없는 실제 장소 이름·평점·영업시간은 지어내지 마세요.
장소 종류가 없는 일정 추가 요청이면 어떤 장소를 원하는지 먼저 물어보세요.
현재 저장된 동행 구성·강도·경비에 맞춰 다음 추천을 작성하세요. 이전 대화에서
다른 조건을 썼더라도 최신 저장값을 기준으로 설명하고, 기존 일정이 자동으로
수정되었다고 말하지 마세요. 경비는 상대적인 선호 수준이며 실제 가격이나 예약
가능 여부를 확인한 값이 아닙니다. 나이나 동행 구성만으로 취향을 단정하지 마세요.
여행지로 지정한 도시 안에서 추천하고, 사용자가 요청하지 않은 근교 도시로 범위를
늘리지 마세요. 숙소 미정은 지역 제한을 없애는 이유가 아닙니다. 도시 안이라는
설명만으로 실제 장소·동선 검증이 완료되었다고 말하지 마세요.
""".strip()

    # Gemini 역할 이름에 맞춰 DB 메시지 역할을 변환한다.
    role_map = {"user": "user", "assistant": "model"}
    contents = [
        {"role": role_map[row["role"]], "parts": [{"text": row["content"]}]}
        for row in history[-12:]
        if row.get("role") in role_map
    ]
    contents.append({"role": "user", "parts": [{"text": user_message}]})
    return api_key, model, system_prompt, contents


def generate_travel_reply(
    trip: dict,
    days: list[dict],
    history: list[dict],
    user_message: str,
    mate_type: str = "assistant",
) -> str:
    """여행 정보, 일정, 최근 대화를 바탕으로 Gemini 답변을 생성한다."""
    api_key, model, system_prompt, contents = _travel_generation_inputs(
        trip, days, history, user_message, mate_type
    )

    try:
        client = genai.Client(api_key=api_key)
        response = client.models.generate_content(
            model=model,
            contents=contents,
            config=types.GenerateContentConfig(system_instruction=system_prompt),
        )
    except Exception as error:
        raise RuntimeError(f"Gemini 응답 생성 실패: {type(error).__name__}: {error}") from error

    if not response.text:
        raise RuntimeError("Gemini가 빈 응답을 반환했습니다.")
    return response.text.strip()


def generate_travel_reply_stream(
    trip: dict,
    days: list[dict],
    history: list[dict],
    user_message: str,
    mate_type: str = "assistant",
) -> Iterator[str]:
    """Gemini 답변을 완성 전 텍스트 조각 단위로 순서대로 반환한다.

    이 함수는 DB에 아무것도 저장하지 않는다. 호출한 채팅 라우터가 모든
    조각을 모아 assistant 메시지 한 건으로 저장하므로, 스트리밍 중에
    메시지가 여러 행으로 쪼개지는 일을 막을 수 있다.
    """

    api_key, model, system_prompt, contents = _travel_generation_inputs(
        trip, days, history, user_message, mate_type
    )
    try:
        client = genai.Client(api_key=api_key)
        for chunk in client.models.generate_content_stream(
            model=model,
            contents=contents,
            config=types.GenerateContentConfig(system_instruction=system_prompt),
        ):
            # 일부 Gemini 이벤트에는 텍스트가 아닌 메타데이터만 들어온다.
            if chunk.text:
                yield chunk.text
    except Exception as error:
        raise RuntimeError(
            f"Gemini 스트리밍 응답 생성 실패: {type(error).__name__}: {error}"
        ) from error
