"""일정표 이미지를 LLM 이 그린다 (SCR-007).

심플형과 일러스트형은 **같은 코드 경로**를 지나고 프롬프트만 다르다
(services/itinerary_export_prompt.py). 스타일이 코드 분기를 만들면 한쪽만
고치는 사고가 나므로, 분기는 프롬프트 선택 한 줄로 끝낸다.

**실패는 전부 None 이다.** 키 권한이 없거나 모델명이 바뀌거나 안전 필터에
걸리면 그림을 못 받는데, 그때 예외를 올리면 다운로드 화면 전체가 멎는다.
부르는 쪽은 None 을 받으면 텍스트 대체 수단을 안내한다.
"""

import base64
import hashlib
import json
import logging
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from google import genai
from google.genai import types

from app.cache import cache_get, cache_set
from app.services.itinerary_export_prompt import (
    DEFAULT_EXPORT_IMAGE_STYLE,
    EXPORT_IMAGE_PROMPTS,
)
from app.services.travel_preferences import TRAVEL_PARTY_LABELS

LOGGER = logging.getLogger(__name__)

# 그린 그림을 들고 있는 시간. 일정이 바뀌면 지문이 달라져 키가 갈리므로
# 오래 둬도 낡은 그림이 나가지 않는다 - 길게 잡는 편이 이득이다.
CACHE_TTL_SECONDS = 7 * 24 * 3600

# 한 장이 이보다 크면 캐시에 넣지 않는다. Redis 에 수 MB 를 밀어 넣어 세션
# 캐시를 밀어내는 것보다, 다음에 다시 그리는 편이 낫다.
MAX_CACHE_BYTES = 4 * 1024 * 1024

# 프롬프트에 싣는 하루당 항목 수. 다 넣으면 모델이 글자로 화면을 채우고,
# 프롬프트가 요구하는 "하루 4~7개" 와도 어긋난다.
MAX_ITEMS_PER_DAY = 7

# 한 장에 담는 일수. 4일 이상을 한 장에 넣으면 모델이 글자를 줄여 넣어
# 장소명이 뭉갠다. 하루 최대 7항목 x 2일 = 14줄이 A4 세로 한 장의 한계선이다.
DAYS_PER_PAGE = 2

# 장수만큼 호출과 비용이 는다. 10일 여행에 5장을 그리지 않고, 넘치는 일자는
# 마지막 장에 몰아넣는다.
MAX_PAGES = 4

# 순차로 부르면 4장에 160초가 걸려 화면이 먼저 끊긴다. 동시에 이만큼까지만
# 부른다 - 쿼터를 한꺼번에 태우지 않으면서 벽시계 시간은 한 장 수준이 된다.
MAX_PARALLEL_PAGES = 4

# 이 .env 의 키로 models.list() 를 확인한 결과 GA 모델 gemini-3-pro-image 가
# 있다. preview 는 이름이 바뀔 수 있으므로 GA 를 기본값으로 둔다.
DEFAULT_IMAGE_MODEL = "gemini-3-pro-image"

# 이미지 생성은 한 번에 20~40초가 걸린다. SDK 기본 타임아웃이 먼저 끊으면
# 다 그린 그림을 버리고 실패로 처리하게 된다. 단위는 밀리초다.
LLM_TIMEOUT_MS = 120_000

# 2장부터 첫 장을 참조 이미지로 함께 보낼 때 덧붙이는 지시다.
#
# **프롬프트 본문에 넣지 않는 이유**는 첫 장에는 붙으면 안 되기 때문이다. 첫 장은
# 참조할 그림이 없는데 "함께 보낸 이미지와 같게" 라고 하면 모델이 없는 것을 찾는다.
_STYLE_REFERENCE_NOTE = """

[Style Reference]

함께 보낸 이미지는 **같은 여행 일정표의 앞 장**입니다.
그 이미지와 다음이 **완전히 같아야** 합니다.

- 종이 색과 질감
- 선의 굵기와 색
- 글씨체와 글자 크기의 위계
- 카드 · 박스의 모양과 모서리, 여백
- 아이콘과 캐릭터의 그림체
- 강조에 쓰는 색과 그 쓰임새
- 제목 영역의 높이와 배치

**내용만 다릅니다.** 위 [Data] 의 일자로 바꿔 그리세요.
앞 장에 이미 그린 날짜와 장소를 다시 그리지 마세요.
새로운 색이나 새로운 그림체를 만들지 마세요."""


# itinerary_items.item_type 은 영문 enum 이다. 그대로 넘기면 일정표에
# 'place' 라고 찍힌다.
_ITEM_TYPE_LABELS = {
    "place": "관광", "cafe": "카페", "restaurant": "식사", "hotel": "숙소",
    "flight": "항공", "train": "기차", "transit": "이동",
    "activity": "체험", "note": "메모",
}


def _clock_text(value: object, timezone_name: object) -> str:
    """저장된 UTC 시각을 여행지 현지 HH:MM 으로 바꾼다.

    routers/trips.py 의 _clock_text 와 같은 규칙이다. 라우터를 import 하면
    순환 참조가 되므로 같은 규칙을 여기에 둔다. 한쪽을 고치면 다른 쪽도 고칠 것.
    """
    try:
        parsed = value if isinstance(value, datetime) else datetime.fromisoformat(
            str(value).replace("Z", "+00:00")
        )
        zone = ZoneInfo(str(timezone_name or "Asia/Seoul"))
        # 시간대가 빠진 과거 데이터는 서버 시간대가 아니라 여행지 벽시계로 읽는다.
        parsed = parsed.replace(tzinfo=zone) if parsed.tzinfo is None else parsed.astimezone(zone)
        return parsed.strftime("%H:%M")
    except (TypeError, ValueError, ZoneInfoNotFoundError):
        return ""


def _ordered_items(day: dict) -> list[dict]:
    """routers/trips.py 의 _ordered_day_items 와 같은 정렬 기준.

    trip_dashboard() 는 sort_order 로만 정렬해 돌려주지만 화면·지도는 시간
    우선으로 보여 준다. 여기서 맞춰 두지 않으면 그림이 화면과 다른 순서로 나온다.
    """
    return sorted(
        day.get("items") or [],
        key=lambda item: (
            item.get("start_at") is None,
            str(item.get("start_at") or ""),
            int(item.get("sort_order") or 0),
        ),
    )


def _plan_fingerprint(trip: dict, days: list[dict]) -> str:
    """일정 내용이 같으면 같은 그림이 나오도록 만드는 캐시 지문.

    trips.updated_at 을 쓰면 안 된다 - touch_trip() 이 채팅 메시지 한 줄에도
    갱신하므로, 대화할 때마다 애써 그린 그림이 버려진다. 반대로 지문이 없으면
    일정을 고쳐도 옛날 그림이 나온다.
    """
    payload = [
        trip.get("title"), trip.get("destination"), trip.get("travel_party"),
        str(trip.get("start_date")), str(trip.get("end_date")), trip.get("timezone"),
        [
            [
                day.get("day_number"), str(day.get("travel_date")),
                [
                    [i.get("id"), i.get("title"), str(i.get("start_at")),
                     i.get("item_type"), i.get("estimated_stay_minutes"),
                     i.get("sort_order")]
                    for i in _ordered_items(day)
                ],
            ]
            for day in days
        ],
    ]
    raw = json.dumps(payload, ensure_ascii=False, default=str, sort_keys=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _cache_key(trip_id: object, style: str, page: int, fingerprint: str) -> str:
    """style 과 page 가 키에 반드시 들어가야 한다.

    style 을 빠뜨리면 심플형을 먼저 받은 사용자가 일러스트형을 눌렀을 때 캐시에
    있던 심플형 그림을 그대로 받는다. 화면은 성공했다고 표시하는데 그림만
    다르므로, 사용자는 자기가 무엇을 잘못 눌렀는지 알 수 없다.

    page 를 빠뜨리면 모든 장이 같은 키를 쓴다. 1장을 그린 뒤 2장을 요청하면
    캐시의 1장이 그대로 나와, 4장짜리 일정표가 같은 그림 4장이 된다.

    지문은 **전체 일정**으로 계산한 값을 받는다. 페이지 내용만으로 계산하면
    3일차를 고쳤을 때 1-2일차 장의 지문이 그대로라 헤더의 기간 표기가 낡은 채
    남는다.
    """
    return f"itinerary_export_image:{trip_id}:{style}:p{page}:{fingerprint}"


def _chunk_days(days: list[dict]) -> list[list[dict]]:
    """일정을 페이지 단위로 자른다.

    **장수는 코드가 정한다.** 모델에게 "여러 장으로 나눠 그려" 라고 하면 한 장
    안에 페이지를 나란히 그리거나 그냥 다 우겨넣는다. 몇 장이 올지 모르면
    화면이 버튼을 몇 개 그릴지도 정할 수 없다.
    """
    if not days:
        return []
    chunks = [days[i:i + DAYS_PER_PAGE] for i in range(0, len(days), DAYS_PER_PAGE)]
    if len(chunks) > MAX_PAGES:
        # 넘치는 일자는 마지막 장에 몰아넣는다. 장수를 늘리면 비용도 같이 는다.
        tail = [day for chunk in chunks[MAX_PAGES - 1:] for day in chunk]
        chunks = chunks[:MAX_PAGES - 1] + [tail]
    return chunks


def _party_text(trip: dict) -> str:
    """동행 구성 한 줄. 인원 수는 이 프로젝트에 저장되지 않는다."""
    party = str(trip.get("travel_party") or "unspecified")
    if party == "unspecified":
        return "동행 구성 미선택"
    return TRAVEL_PARTY_LABELS.get(party, "동행 구성 미선택")


def _period_text(trip: dict) -> str:
    """기간 한 줄. 날짜가 없으면 시각을 지어내지 말라고 못박는다."""
    if not trip.get("start_date"):
        return "기간: 날짜 미정 (시각 대신 순서로 그리세요)"
    start = str(trip.get("start_date") or "")
    end = str(trip.get("end_date") or "")
    return f"기간: {start} ~ {end}".strip(" ~")


def _plan_text(trip: dict, days: list[dict]) -> str:
    """모델에게 보낼 일정. 하루 앞 몇 개만, 한 줄에 하나씩."""
    timezone_name = trip.get("timezone") or "Asia/Seoul"
    undecided = not trip.get("start_date")
    lines: list[str] = []
    for day in days:
        header = f"[{day.get('day_number')}일차]"
        if day.get("travel_date") and not undecided:
            header += f" {day['travel_date']}"
        lines.append(header)
        for index, item in enumerate(_ordered_items(day)[:MAX_ITEMS_PER_DAY], start=1):
            when = "" if undecided else _clock_text(item.get("start_at"), timezone_name)
            when = when or f"{index}번째"
            category = _ITEM_TYPE_LABELS.get(str(item.get("item_type") or ""), "일정")
            stay = item.get("estimated_stay_minutes")
            tail = f" {stay}분" if stay else ""
            lines.append(f"  {when} {item.get('title')} · {category}{tail}")
    return "\n".join(lines)


def build_prompt(
    trip: dict,
    days: list[dict],
    style: str,
    *,
    page: int = 1,
    total_pages: int = 1,
) -> str:
    """스타일에 맞는 프롬프트에 이 여행의 실제 값을 채워 넣는다.

    코드의 스타일 분기는 여기 한 줄이 전부다. 아래로는 두 스타일이 완전히
    같은 경로를 지난다.

    `days` 는 **이 장에 그릴 일자만** 담은 조각이다. 그런데 헤더의 기간은
    여행 전체라, 모델이 받지 않은 날짜를 지어내 채우는 일이 생긴다.
    머리말로 "이 일자만" 을 못박는다.
    """
    template = EXPORT_IMAGE_PROMPTS.get(style)
    if template is None:
        # 라우터의 Literal 이 먼저 막지만, 서비스를 다른 곳에서 부를 때를 대비해
        # 조용히 빈 그림을 내지 않고 여기서 드러나게 한다.
        raise ValueError(f"지원하지 않는 일정표 스타일입니다: {style}")
    plan = _plan_text(trip, days)
    if total_pages > 1:
        plan = (
            f"({total_pages}장 중 {page}장) 아래 일자만 이 장에 그리세요. "
            "다른 장의 일자를 끌어오지 마세요.\n"
            f"{plan}"
        )
    return template.format(
        title=trip.get("title") or "여행 일정",
        period=_period_text(trip),
        party=_party_text(trip),
        plan=plan,
    )


def _first_image(response) -> bytes | None:
    """응답에서 첫 이미지 파트를 꺼낸다.

    이미지 모델은 그림과 설명 문장을 **섞어서** 준다. 파트를 순서대로 훑어
    inline_data 가 있는 첫 조각만 쓴다 - 없으면 (안전 필터에 걸렸거나 모델이
    글로만 답했거나) None 이다.
    """
    for candidate in getattr(response, "candidates", None) or []:
        content = getattr(candidate, "content", None)
        for part in getattr(content, "parts", None) or []:
            data = getattr(getattr(part, "inline_data", None), "data", None)
            if not data:
                continue
            # google-genai 2.22.0 의 Blob.data 는 bytes 로 선언돼 있어 아래 str
            # 분기는 사실상 방어 코드다. SDK 가 바뀌었을 때 조용히 깨지는 것보다
            # 낫기에 남긴다.
            if isinstance(data, str):
                try:
                    return base64.b64decode(data)
                except Exception:
                    LOGGER.warning("이미지 파트를 못 읽었습니다 - 다음 파트를 봅니다.")
                    continue
            return bytes(data)
    return None


def _generate_one(
    client,
    model: str,
    trip: dict,
    chunk: list[dict],
    style: str,
    page: int,
    total_pages: int,
    fingerprint: str,
    reference: bytes | None = None,
) -> bytes | None:
    """한 장을 그린다. 못 그리면 None - **예외를 올리지 않는다.**

    `reference` 는 앞서 그린 **첫 장**이다. 함께 보내면 모델이 그 그림을 보고
    같은 톤으로 그린다. 없으면(첫 장이거나 첫 장이 실패했으면) 프롬프트만 보낸다.
    """

    key = _cache_key(trip["id"], style, page, fingerprint)
    if cached := cache_get(key):
        try:
            return base64.b64decode(cached)
        except Exception:
            LOGGER.warning("일정표 그림 캐시가 깨졌습니다 - 다시 그립니다.")

    prompt = build_prompt(trip, chunk, style, page=page, total_pages=total_pages)
    if reference is not None:
        prompt += _STYLE_REFERENCE_NOTE
    # 이미지 모델은 그림과 글을 함께 낼 수 있다. 둘 다 받아 두고 그림만 골라
    # 쓴다 - TEXT 를 빼면 일부 모델이 요청 자체를 거부한다.
    config = types.GenerateContentConfig(response_modalities=["TEXT", "IMAGE"])

    # 그림을 먼저, 지시를 뒤에 둔다 - 모델이 "이것을 보고 저렇게 하라" 로 읽는다.
    contents: list = [prompt]
    if reference is not None:
        contents = [types.Part.from_bytes(data=reference, mime_type="image/png"), prompt]

    png: bytes | None = None
    # **한 번은 다시 건다.** 같은 프롬프트로 같은 모델을 불러도 어떤 회차는
    # 그림 없이 짧은 글만 돌아온다. 재시도 없이 실패로 처리하면 사용자는
    # 이유를 알 수 없는 503 을 받는다.
    for attempt in (1, 2):
        try:
            response = client.models.generate_content(
                model=model, contents=contents, config=config)
        except Exception as error:
            # **호출 자체가 실패하면 접는다.** 모델명이 바뀌었거나 이 키에 이미지
            # 생성 권한이 없으면 매번 같은 실패가 나는데, 다시 걸면 시간만 두 배로
            # 태운다. 이 저장소 관례대로 예외 원문 대신 종류만 남긴다.
            LOGGER.warning("일정표 그림 생성 실패 (style=%s, page=%s, model=%s, %s).",
                           style, page, model, type(error).__name__)
            return None

        if png := _first_image(response):
            break
        if attempt == 1:
            LOGGER.info("일정표 그림이 응답에 없습니다 - 한 번 다시 겁니다 "
                        "(trip=%s, page=%s).", trip.get("id"), page)

    if not png:
        LOGGER.warning("일정표 그림을 두 번 다 못 받았습니다 (trip=%s, style=%s, page=%s).",
                       trip.get("id"), style, page)
        return None

    if len(png) <= MAX_CACHE_BYTES:
        cache_set(key, base64.b64encode(png).decode("ascii"), CACHE_TTL_SECONDS)
    return png


def generate_itinerary_images(
    trip: dict,
    days: list[dict],
    style: str = DEFAULT_EXPORT_IMAGE_STYLE,
) -> list[bytes | None]:
    """일정을 페이지로 나눠 장마다 PNG 를 그린다.

    **길이 N 리스트를 돌려준다.** 실패한 장은 그 자리에 None 이 남는다. 4장 중
    2장만 성공했다고 전부 버리면 사용자는 아무것도 못 받는데, 자리를 비워 두면
    부르는 쪽이 "4장 중 2장" 을 알 수 있고 파일 이름의 2of4 도 실제 순서와 맞는다.

    지문은 **전체 일정**으로 한 번 계산해 모든 장이 함께 쓴다 (_cache_key 주석).

    **첫 장을 먼저 그리고 나머지는 그것을 보며 그린다.** 장마다 독립으로 부르면
    모델이 매번 처음부터 해석해 종이색 · 글씨체 · 그림체가 장마다 갈렸다. 대가는
    시간이다 - 전부 병렬이면 40초, 첫 장을 기다리면 80초쯤 걸린다. 순차로 네 번
    부르는 160초보다는 짧고 화면 타임아웃(150초) 안이다.
    """

    chunks = _chunk_days(days)
    if not chunks:
        return []

    api_key = os.getenv("GEMINI_API_KEY", "").strip()
    if not api_key:
        # 채팅과 같은 설정을 쓰므로 여기서 별도 오류를 만들지 않는다.
        return []
    model = os.getenv("GEMINI_IMAGE_MODEL", "").strip() or DEFAULT_IMAGE_MODEL

    # 클라이언트는 한 번만 만들어 모든 장이 함께 쓴다.
    client = genai.Client(api_key=api_key, http_options={"timeout": LLM_TIMEOUT_MS})
    fingerprint = _plan_fingerprint(trip, days)
    total_pages = len(chunks)

    # 첫 장이 나머지 장의 기준이 된다. 이것만 순서를 기다린다.
    first = _generate_one(client, model, trip, chunks[0], style, 1, total_pages, fingerprint)
    if total_pages == 1:
        return [first]

    if first is None:
        # 기준이 없다. 통일은 포기하고 나머지라도 건진다 - 첫 장이 실패했다고
        # 2~4장까지 버리면 사용자는 아무것도 못 받는다.
        LOGGER.warning("첫 장을 못 그려 나머지 장의 톤을 맞추지 못합니다 (trip=%s).",
                       trip.get("id"))
        rest_fingerprint = fingerprint
    else:
        # **참조 이미지를 캐시 키에 반영한다.** 첫 장이 다시 그려지면 색과 그림체가
        # 달라지는데, 2~4장의 키가 그대로면 옛 첫 장에 맞춰 그린 그림이 나온다.
        # 그러면 통일하려던 것이 오히려 어긋난다.
        rest_fingerprint = f"{fingerprint}-{hashlib.sha256(first).hexdigest()[:8]}"

    # 나머지 장은 서로 독립이므로 병렬로 부른다.
    with ThreadPoolExecutor(
        max_workers=min(total_pages - 1, MAX_PARALLEL_PAGES)
    ) as pool:
        futures = [
            pool.submit(_generate_one, client, model, trip, chunk, style,
                        page, total_pages, rest_fingerprint, first)
            for page, chunk in enumerate(chunks[1:], start=2)
        ]
        return [first] + [future.result() for future in futures]


def itinerary_as_text(trip: dict, days: list[dict]) -> str:
    """이미지를 못 만들었을 때의 대체 수단.

    이미지를 못 만들어도 **일정 자체는 있으므로** 붙여넣을 수 있는 텍스트를 준다.
    """
    timezone_name = trip.get("timezone") or "Asia/Seoul"
    lines = [str(trip.get("title") or "여행 일정")]
    if trip.get("start_date"):
        lines.append(f"{trip['start_date']} ~ {trip.get('end_date') or ''}".strip(" ~"))

    for day in days:
        lines.append("")
        header = f"[{day.get('day_number')}일차]"
        if day.get("travel_date"):
            header += f" {day['travel_date']}"
        lines.append(header)
        for index, item in enumerate(_ordered_items(day), start=1):
            when = _clock_text(item.get("start_at"), timezone_name) or f"{index}번째"
            category = _ITEM_TYPE_LABELS.get(str(item.get("item_type") or ""), "일정")
            stay = item.get("estimated_stay_minutes")
            tail = f" {stay}분" if stay else ""
            lines.append(f"  {when}  {item.get('title')}  · {category}{tail}")
    return "\n".join(lines)