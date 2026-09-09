from fastapi import APIRouter, HTTPException

from app.db import get_anon_client, get_service_client
from app.schemas import (
    PasswordResetRequest,
    LoginRequest,
    MessageResponse,
    SignupRequest,
    TokenResponse,
)

router = APIRouter(prefix="/auth", tags=["auth"])


def _find_user_by_email(service_client, email: str):
    """서버 전용 Auth API로 이메일에 해당하는 사용자를 찾는다.

    `profiles`에는 의도적으로 이메일 주소를 저장하지 않으므로, 먼저 Supabase
    Auth에서 이메일을 찾고 이후 profiles에서 사용자 이름을 확인한다.
    """

    result = service_client.auth.admin.list_users(page=1, per_page=1000)
    users = getattr(result, "users", result)
    normalized_email = email.casefold()
    return next(
        (
            user
            for user in users
            if (getattr(user, "email", "") or "").casefold() == normalized_email
        ),
        None,
    )


@router.post(
    "/signup", response_model=TokenResponse, summary="회원가입",
    response_description="생성된 계정 ID, 이메일, 액세스 토큰",
    responses={400: {"description": "이미 사용 중인 이메일, 잘못된 가입 정보 또는 가입 처리 실패"}, 503: {"description": "Supabase 인증 서비스 설정 또는 연결 문제"}},
)
def signup(payload: SignupRequest):
    """Supabase Auth 계정을 만들고 가능하면 최초 세션을 반환한다."""

    try:
        # public/anon 클라이언트는 계정을 만들 수 있지만 관리자 작업은 할 수 없다.
        # Supabase는 데이터베이스 프로필 생성 트리거를 위해 이 메타데이터를 저장한다.
        result = get_anon_client().auth.sign_up(
            {
                "email": str(payload.email),
                "password": payload.password,
                "options": {"data": {"username": payload.username}},
            }
        )
    except RuntimeError as error:
        raise HTTPException(status_code=503, detail=str(error)) from error
    except Exception as error:
        raise HTTPException(status_code=400, detail=str(error)) from error

    if result.user is None:
        raise HTTPException(status_code=400, detail="회원가입 결과를 확인할 수 없습니다.")

    return TokenResponse(
        access_token=result.session.access_token if result.session else None,
        user_id=str(result.user.id),
        email=result.user.email or str(payload.email),
    )


@router.post(
    "/login", response_model=TokenResponse, summary="로그인",
    response_description="액세스 토큰과 로그인 사용자 정보",
    responses={401: {"description": "이메일 또는 비밀번호가 일치하지 않음"}, 503: {"description": "Supabase 인증 서비스 설정 또는 연결 문제"}},
)
def login(payload: LoginRequest):
    """이메일·비밀번호 조합을 인증하고 이후 API 호출에 쓸 Bearer 토큰을 반환한다."""

    try:
        # 반환한 접근 토큰은 이후 프론트엔드가 Bearer 토큰으로 보낸다.
        result = get_anon_client().auth.sign_in_with_password(
            {"email": str(payload.email), "password": payload.password}
        )
    except RuntimeError as error:
        raise HTTPException(status_code=503, detail=str(error)) from error
    except Exception as error:
        raise HTTPException(status_code=401, detail="이메일 또는 비밀번호를 확인하세요.") from error

    if result.user is None or result.session is None:
        raise HTTPException(status_code=401, detail="로그인 정보를 확인할 수 없습니다.")

    return TokenResponse(
        access_token=result.session.access_token,
        user_id=str(result.user.id),
        email=result.user.email or str(payload.email),
    )


@router.post("/password-reset/demo", response_model=MessageResponse, summary="비밀번호 재설정")
def reset_password(payload: PasswordResetRequest):
    """프로필 사용자 이름과 Auth 이메일이 일치할 때 비밀번호를 재설정한다.

    운영 환경에서는 이메일 소유 인증 또는 별도 본인 인증 절차를 함께 적용해야
    한다. Supabase의 복구 이메일 흐름을 사용하는 방식을 권장한다.
    """

    try:
        # Auth 사용자 검색과 비밀번호 변경에 필요한 service-role 키는 서버만
        # 보관한다. 이 키는 Streamlit 앱에 절대 노출하면 안 된다.
        service_client = get_service_client()
        user = _find_user_by_email(service_client, str(payload.email))
        if user is None:
            raise ValueError("No matching user")

        profile_result = (
            service_client.table("profiles")
            .select("username")
            .eq("id", str(user.id))
            .single()
            .execute()
        )
        profile = profile_result.data
        stored_username = (profile or {}).get("username", "").strip()
        if stored_username != payload.username.strip():
            raise ValueError("Username does not match")

        # Supabase Auth가 새 비밀번호를 해시해 저장하며, 이 앱은 SQL로
        # auth.users에 직접 쓰지 않는다.
        service_client.auth.admin.update_user_by_id(
            str(user.id), {"password": payload.new_password}
        )
    except RuntimeError as error:
        raise HTTPException(status_code=503, detail=str(error)) from error
    except Exception as error:
        raise HTTPException(
            status_code=400,
            detail="이름 또는 이메일을 확인하세요.",
        ) from error

    return MessageResponse(message="비밀번호가 변경되었습니다. 새 비밀번호로 로그인하세요.")
