"""Gemini는 문장 작성과 여행 안내에만 사용하며, 데이터베이스 권한 확인에는 쓰지 않는다."""

import os
from collections.abc import Iterator
from datetime import datetime
from zoneinfo import ZoneInfo

from google import genai
from google.genai import types

from app.services.travel_preferences import travel_preferences_text


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
    trip: dict, days: list[dict], history: list[dict], user_message: str
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
    system_prompt = f"""
당신은 TripMate의 AI 여행 플래너입니다.
여행 제목: {trip.get('title')}
여행지: {trip.get('destination') or '미정'}
시간대: {trip.get('timezone') or 'Asia/Seoul'}

현재 저장된 여행 조건:
{travel_preferences_text(trip)}

현재 확정된 일정:
{_schedule_summary(days, trip.get('timezone') or 'Asia/Seoul')}

한국어로 친절하고 짧게 답하세요. 일정에 없는 사실, 실제 영업시간,
실시간 교통시간을 지어내지 마세요. 사용자가 일정 추가를 요청하면
현재는 사용자가 화면의 '일정 직접 추가'에서 확정할 수 있다고 안내하세요.
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
    trip: dict, days: list[dict], history: list[dict], user_message: str
) -> str:
    """여행 정보, 일정, 최근 대화를 바탕으로 Gemini 답변을 생성한다."""
    api_key, model, system_prompt, contents = _travel_generation_inputs(
        trip, days, history, user_message
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
    trip: dict, days: list[dict], history: list[dict], user_message: str
) -> Iterator[str]:
    """Gemini 답변을 완성 전 텍스트 조각 단위로 순서대로 반환한다.

    이 함수는 DB에 아무것도 저장하지 않는다. 호출한 채팅 라우터가 모든
    조각을 모아 assistant 메시지 한 건으로 저장하므로, 스트리밍 중에
    메시지가 여러 행으로 쪼개지는 일을 막을 수 있다.
    """

    api_key, model, system_prompt, contents = _travel_generation_inputs(
        trip, days, history, user_message
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
