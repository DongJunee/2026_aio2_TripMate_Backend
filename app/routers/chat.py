import json
import logging
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse

from app.activity_logging import record_activity
from app.cache import cache_delete, cache_get, cache_set
from app.db import get_user_client
from app.deps import CurrentUser, get_current_user, require_own_trip
from app.gemini_client import generate_travel_reply_stream, resolve_itinerary_place_swap_request
from app.routers.trips import apply_ai_place_swap, touch_trip, trip_dashboard
from app.schemas import ChatRequest

router = APIRouter(tags=["chat"])
MESSAGES_CACHE_TTL_SECONDS = 300
LOGGER = logging.getLogger(__name__)


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
    mate_type: str,
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
                dashboard["trip"], dashboard["days"], history, user_message, mate_type
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


def _stream_fixed_answer(
    client,
    trip_id: UUID,
    text: str,
) -> StreamingResponse:
    """서버가 실제 일정 작업을 끝낸 뒤 짧은 확인 답변을 저장·전송한다."""

    def event_stream():
        """변경 완료 문구와 저장 완료 이벤트를 SSE 순서로 만든다."""

        try:
            saved = (
                client.table("messages")
                .insert({"trip_id": str(trip_id), "role": "assistant", "content": text})
                .execute()
            )
            cache_delete(_cache_key(trip_id))
        except Exception as error:
            yield _sse_event({"error": f"AI 답변을 저장하지 못했습니다. {error}"})
            return
        yield _sse_event({"text": text})
        saved_id = saved.data[0]["id"] if saved.data else None
        yield _sse_event({"done": True, "message_id": saved_id})

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
    profile_result = client.table("profiles").select("mate_type").eq("id", current_user.id).execute()
    profile = profile_result.data[0] if profile_result.data else {}
    # SQL 반영 전이거나 기존 프로필에 값이 없으면 기존과 같은 비서 방식으로 답한다.
    mate_type = str(profile.get("mate_type") or "assistant")

    user_message = (
        client.table("messages")
        .insert({"trip_id": str(trip_id), "role": "user", "content": payload.content})
        .execute()
    )
    record_activity(
        client,
        user_id=current_user.id,
        event_type="chat.send",
        trip_id=trip_id,
        entity_type="message",
        entity_id=user_message.data[0].get("id") if user_message.data else None,
        metadata={"message_length": len(payload.content)},
    )
    # 방금 저장한 사용자 메시지가 기록을 바꾸므로 즉시 캐시를 무효화한다.
    cache_delete(_cache_key(trip_id))
    # 새 채팅도 여행 활동이므로, 일반 여행 사이드바 정렬은 여행의 최근 상호작용
    # 시간에 따라 바뀐다.
    touch_trip(client, trip_id)

    # '오늘 점심 식당과 저녁 식당을 바꿔줘'처럼 명확한 요청만 Gemini가 현재
    # 일정 ID 두 개로 해석한다. 모델은 ID 선택만 하고, 실제 변경은 아래의 RLS
    # 보호 함수가 같은 여행·같은 DAY인지 다시 확인한 뒤 처리한다.
    try:
        swap_request = resolve_itinerary_place_swap_request(
            dashboard["trip"], dashboard["days"], payload.content
        )
        if swap_request:
            changed = apply_ai_place_swap(client, trip_id, *swap_request)
            change = changed["change"]
            return _stream_fixed_answer(
                client,
                trip_id,
                f"요청대로 {change['message']} 아래 일정 변경 카드에서 되돌릴 수도 있어요.",
            )
    except HTTPException as error:
        # 대상이 같은 DAY가 아니거나 이미 삭제된 일정처럼 안전하게 처리할 수 없는
        # 경우에는 기존 채팅 답변으로 이어간다. 사용자 메시지만 저장된 채 끝나는
        # 일은 피하고, DB 변경도 이 경로에서는 일어나지 않는다.
        LOGGER.info("AI 일정 순서 변경을 적용하지 않았습니다: %s", error.detail)
    except Exception as error:
        # 구조화 해석은 보조 기능이다. 이 기능의 일시적 오류가 일반 채팅 자체를
        # 막지 않도록 로그만 남기고 아래의 기존 스트리밍 답변으로 진행한다.
        LOGGER.warning("AI 일정 순서 변경 해석 실패 (%s).", type(error).__name__)

    # 이 지점부터 오류는 이미 열린 SSE 스트림 안에서 전달한다.
    return _stream_answer(client, trip_id, dashboard, history, payload.content, mate_type)

@router.post("/trips/{trip_id}/chat/reset-context")
def delete_chat_history(
    trip_id: UUID = Depends(require_own_trip),
    current_user: CurrentUser = Depends(get_current_user),
):
    """로그인한 사용자의 해당 여행 채팅 기록을 DB와 캐시에서 모두 삭제한다."""

    client = get_user_client(current_user.token)
    # require_own_trip가 먼저 소유권을 확인하고, RLS도 본인 여행의 메시지만
    # 삭제하도록 한 번 더 제한한다. 일정·여행 정보는 이 요청으로 삭제하지 않는다.
    (
        client.table("messages")
        .delete()
        .eq("trip_id", str(trip_id))
        .execute()
    )
    cache_delete(_cache_key(trip_id))
    touch_trip(client, trip_id)
    return {"detail": "대화 기록을 모두 삭제했습니다."}
