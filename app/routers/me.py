from fastapi import APIRouter, Depends, HTTPException

from app.db import get_user_client
from app.deps import CurrentUser, get_current_user
from app.schemas import ProfileUpdate

router = APIRouter(prefix="/me", tags=["me"])


@router.get("")
def read_me(current_user: CurrentUser = Depends(get_current_user)):
    """인증된 사용자의 Auth 식별 정보와 서비스 프로필을 반환한다."""

    # 사용자 토큰을 사용해 Supabase RLS가 본인 프로필로 조회를 제한하게 한다.
    client = get_user_client(current_user.token)
    result = client.table("profiles").select("*").eq("id", current_user.id).execute()
    profile = result.data[0] if result.data else None
    return {"id": current_user.id, "email": current_user.email, "profile": profile}


@router.patch("/profile")
def update_profile(
    payload: ProfileUpdate,
    current_user: CurrentUser = Depends(get_current_user),
):
    """로그인한 사용자의 표시용 사용자 이름을 변경한다."""

    # ID는 클라이언트 입력이 아니라 검증된 Bearer 토큰에서만 가져온다.
    client = get_user_client(current_user.token)
    result = (
        client.table("profiles")
        .update({"username": payload.username})
        .eq("id", current_user.id)
        .execute()
    )
    if not result.data:
        raise HTTPException(status_code=404, detail="프로필을 찾을 수 없습니다.")
    return result.data[0]
