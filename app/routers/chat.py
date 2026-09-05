import json
from uuid import UUID

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse

from app.cache import cache_delete, cache_get, cache_set
from app.db import get_user_client
from app.deps import CurrentUser, get_current_user, require_own_trip
from app.gemini_client import generate_travel_reply_stream
from app.routers.trips import touch_trip, trip_dashboard
from app.schemas import ChatRequest

router = APIRouter(tags=["chat"])
MESSAGES_CACHE_TTL_SECONDS = 300


def _cache_key(trip_id: UUID | str) -> str:
    """여행 하나의 메시지 기록에 쓰는 Redis 캐시 키를 반환한다."""

    return f"trip_messages:{trip_id}"


def _messages(client, trip_id: UUID | str) -> list[dict]:
    """소유권 확인 후 캐시 또는 Supabase에서 여행의 메시지를 반환한다."""

    # 먼저 대시보드 라우터의 RLS 보호 조회로 여행 소유권을 확인한다.
    trip_dashboard(client, trip_id)
    cached = cache_get(_cache_key(trip_id))
    if cached:
        # Redis 값은 직렬화된 문자열이므로 캐시에는 JSON을 저장한다.
        return json.loads(cached)

    data = (
        client.table("messages")
        .select("*")
        .eq("trip_id", str(trip_id))
        .order("created_at")
        .execute()
        .data
    )
    # 캐시는 짧게만 읽고, 아래의 쓰기 동작마다 명시적으로 무효화한다. 그래야 다음
    # 요청에서 오래된 채팅 기록이 보이지 않는다.
    cache_set(_cache_key(trip_id), json.dumps(data, default=str), MESSAGES_CACHE_TTL_SECONDS)
    return data


def _sse_event(payload: dict) -> str:
    """SSE 한 건을 만든다. JSON 인코딩으로 줄바꿈이 이벤트를 끊지 않게 한다."""

    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


def _stream_answer(
    client,
    trip_id: UUID,
    dashboard: dict,
    history: list[dict],
    user_message: str,
) -> StreamingResponse:
    """여행 AI 답변을 SSE로 보내고, 성공한 전체 답변만 DB에 저장한다.

    사용자 메시지는 이 함수를 호출하기 전에 이미 저장되어 있다. 스트림 도중
    Gemini 또는 DB 저장에 실패하면 HTTP 상태 코드를 다시 바꿀 수 없으므로,
    프론트엔드가 읽을 수 있는 ``error`` SSE 이벤트로 오류를 보낸다.
    """

    def event_stream():
        """Gemini 조각 이벤트 뒤에 저장 완료 이벤트를 차례로 생성한다."""

        full_text = ""
        try:
            for text in generate_travel_reply_stream(
                dashboard["trip"], dashboard["days"], history, user_message
            ):
                full_text += text
                yield _sse_event({"text": text})
        except Exception as error:
            # 프론트엔드가 사용자용 접두 문구를 붙이므로, 같은 문장이 두 번 보이지
            # 않게 이 이벤트의 상세값은 그대로 둔다.
            yield _sse_event({"error": str(error)})
            return

        if not full_text.strip():
            yield _sse_event({"error": "Gemini가 빈 응답을 반환했습니다."})
            return

        try:
            # 조각마다 저장하지 않고 완성된 답변을 assistant 메시지 한 건으로 저장한다.
            saved = (
                client.table("messages")
                .insert(
                    {
                        "trip_id": str(trip_id),
                        "role": "assistant",
                        "content": full_text,
                    }
                )
                .execute()
            )
            # 답변이 추가됐으므로 다음 메시지 조회는 DB에서 새 목록을 읽게 한다.
            cache_delete(_cache_key(trip_id))
        except Exception as error:
            yield _sse_event({"error": f"AI 답변을 저장하지 못했습니다. {error}"})
            return

        saved_id = saved.data[0]["id"] if saved.data else None
        yield _sse_event({"done": True, "message_id": saved_id})

    # 프록시가 SSE를 버퍼링하면 글자가 한꺼번에 보일 수 있어 이를 명시적으로 막는다.
    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/trips/{trip_id}/messages")
def list_messages(
    trip_id: UUID = Depends(require_own_trip),
    current_user: CurrentUser = Depends(get_current_user),
):
    """여행·채팅방 소유권 확인이 성공한 뒤에만 메시지 목록을 반환한다."""

    return _messages(get_user_client(current_user.token), trip_id)


@router.post("/trips/{trip_id}/chat")
def chat(
    payload: ChatRequest,
    trip_id: UUID = Depends(require_own_trip),
    current_user: CurrentUser = Depends(get_current_user),
):
    """여행·채팅방을 확인한 뒤 사용자의 AI 채팅을 스트리밍하고 저장한다."""

    client = get_user_client(current_user.token)
    # `_messages`는 데이터베이스 쓰기나 Gemini 호출보다 먼저 여행 접근 권한을 확인한다.
    history = _messages(client, trip_id)
    dashboard = trip_dashboard(client, trip_id)

    user_message = (
        client.table("messages")
        .insert({"trip_id": str(trip_id), "role": "user", "content": payload.content})
        .execute()
    )
    # 방금 저장한 사용자 메시지가 기록을 바꾸므로 즉시 캐시를 무효화한다.
    cache_delete(_cache_key(trip_id))
    # 새 채팅도 여행 활동이므로, 일반 여행 사이드바 정렬은 여행의 최근 상호작용
    # 시간에 따라 바뀐다.
    touch_trip(client, trip_id)

    # 이 지점부터 오류는 이미 열린 SSE 스트림 안에서 전달한다.
    return _stream_answer(client, trip_id, dashboard, history, payload.content)
