from fastapi import APIRouter, Depends, HTTPException

from app.activity_logging import record_activity
from app.db import get_anon_client, get_service_client, get_user_client
from app.dashboard_auth import is_dashboard_admin
from app.deps import CurrentUser, get_current_user
from app.schemas import MessageResponse, PasswordChangeRequest, ProfileUpdate

router = APIRouter(prefix="/me", tags=["me"])


def _verify_current_password(current_user: CurrentUser, password: str) -> None:
    """현재 로그인 이메일과 비밀번호를 다시 확인해 민감한 계정 작업을 보호한다."""

    try:
        result = get_anon_client().auth.sign_in_with_password(
            {"email": current_user.email, "password": password}
        )
    except Exception as error:
        raise HTTPException(status_code=400, detail="현재 비밀번호를 확인하세요.") from error
    if result.user is None or str(result.user.id) != current_user.id:
        raise HTTPException(status_code=400, detail="현재 비밀번호를 확인하세요.")


@router.get("", summary="내 정보 조회")
def read_me(current_user: CurrentUser = Depends(get_current_user)):
    """인증된 사용자의 Auth 식별 정보와 서비스 프로필을 반환한다."""

    # 사용자 토큰을 사용해 Supabase RLS가 본인 프로필로 조회를 제한하게 한다.
    client = get_user_client(current_user.token)
    result = client.table("profiles").select("*").eq("id", current_user.id).execute()
    profile = result.data[0] if result.data else None
    return {
        "id": current_user.id,
        "email": current_user.email,
        "profile": profile,
        "is_dashboard_admin": is_dashboard_admin(profile),
    }


@router.patch(
    "/profile", summary="내 프로필과 Mate 방식 변경",
    response_description="변경 후 현재 사용자의 프로필",
    responses={401: {"description": "Bearer 토큰이 없거나 유효하지 않음"}, 404: {"description": "현재 사용자의 프로필을 찾을 수 없음"}},
)
def update_profile(
    payload: ProfileUpdate,
    current_user: CurrentUser = Depends(get_current_user),
):
    """로그인한 사용자의 표시 이름 또는 Mate 대화 방식을 변경한다."""

    # ID는 클라이언트 입력이 아니라 검증된 Bearer 토큰에서만 가져온다.
    client = get_user_client(current_user.token)
    # 실제로 저장하는 값과 로그에 남기는 필드 이름을 같은 dict에서 가져온다.
    # 두 곳에 따로 적어 두면 mate_type만 바꿔도 username으로 기록되는 식으로
    # 어긋난다.
    values = payload.model_dump(exclude_none=True)
    result = (
        client.table("profiles")
        .update(values)
        .eq("id", current_user.id)
        .execute()
    )
    if not result.data:
        raise HTTPException(status_code=404, detail="프로필을 찾을 수 없습니다.")
    record_activity(
        client,
        user_id=current_user.id,
        event_type="profile.update",
        entity_type="profile",
        entity_id=current_user.id,
        metadata={"fields": sorted(values)},
    )
    return result.data[0]


@router.post("/password", response_model=MessageResponse, summary="비밀번호 변경")
def change_password(
    payload: PasswordChangeRequest,
    current_user: CurrentUser = Depends(get_current_user),
):
    """현재 비밀번호를 확인한 로그인 사용자만 새 비밀번호를 저장한다."""

    if payload.current_password == payload.new_password:
        raise HTTPException(status_code=400, detail="새 비밀번호는 현재 비밀번호와 달라야 합니다.")
    _verify_current_password(current_user, payload.current_password)
    try:
        # service-role 키는 백엔드에만 있고, 브라우저에는 절대 전달되지 않는다.
        get_service_client().auth.admin.update_user_by_id(
            current_user.id, {"password": payload.new_password}
        )
    except RuntimeError as error:
        raise HTTPException(status_code=503, detail=str(error)) from error
    except Exception as error:
        raise HTTPException(status_code=400, detail="비밀번호를 변경하지 못했습니다.") from error
    return MessageResponse(message="비밀번호가 변경되었습니다. 새 비밀번호로 다시 로그인하세요.")
