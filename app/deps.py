"""보호된 라우터가 함께 사용하는 인증 의존성이다."""

import hashlib
import json
from dataclasses import dataclass
from uuid import UUID

from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.cache import cache_get, cache_set
from app.db import get_anon_client, get_user_client

# 토큰이 없을 때 직접 한국어 오류 메시지를 만들기 위해 FastAPI 자동 오류를 끈다.
bearer_scheme = HTTPBearer(auto_error=False)
# Supabase 토큰 검증 결과를 5분 동안 Redis에 보관해 API 호출을 가볍게 한다.
SESSION_CACHE_TTL_SECONDS = 300


@dataclass
class CurrentUser:
    """보호된 API에서 사용하는 현재 로그인 사용자 정보이다."""

    id: str
    email: str
    token: str


def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
) -> CurrentUser:
    """Bearer 토큰을 검증하고, 성공 시 현재 사용자 정보를 반환한다."""
    if credentials is None:
        raise HTTPException(status_code=401, detail="로그인이 필요합니다.")

    token = credentials.credentials
    cache_key = f"session:{hashlib.sha256(token.encode()).hexdigest()}"
    cached = cache_get(cache_key)
    if cached:
        data = json.loads(cached)
        return CurrentUser(id=data["id"], email=data["email"], token=token)

    try:
        user = get_anon_client().auth.get_user(token).user
    except Exception as error:
        raise HTTPException(status_code=401, detail="유효하지 않은 로그인입니다.") from error

    if user is None or not user.email:
        raise HTTPException(status_code=401, detail="유효하지 않은 로그인입니다.")

    current_user = CurrentUser(id=str(user.id), email=user.email, token=token)
    cache_set(
        cache_key,
        json.dumps({"id": current_user.id, "email": current_user.email}),
        SESSION_CACHE_TTL_SECONDS,
    )
    return current_user


def require_own_trip(
    trip_id: UUID,
    current_user: CurrentUser = Depends(get_current_user),
) -> UUID:
    """로그인한 사용자가 접근할 수 있을 때만 ``trip_id``를 반환한다.

    TripMate는 여행 하나당 채팅방 하나를 사용하므로, 이 함수는 여행 플래너의
    대화방 소유권 확인 의존성과 같다. service-role 클라이언트 대신 사용자 JWT가
    연결된 클라이언트로 조회하므로, Supabase RLS가 다른 사용자의 여행을 숨긴다.
    존재하지 않는 여행과 다른 사용자 여행 모두 같은 404 응답을 받는다.
    """

    client = get_user_client(current_user.token)
    result = (
        client.table("trips")
        .select("id")
        .eq("id", str(trip_id))
        .limit(1)
        .execute()
    )
    if not result.data:
        raise HTTPException(status_code=404, detail="여행을 찾을 수 없습니다.")
    return trip_id
