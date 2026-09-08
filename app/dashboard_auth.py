"""TripMate 운영 대시보드 접근 권한을 판정하는 공통 함수."""

from __future__ import annotations

import os


def dashboard_auth_disabled() -> bool:
    """로컬 테스트용 관리자 인증 우회 설정을 반환한다."""

    return os.getenv("DASHBOARD_AUTH_DISABLED", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def configured_dashboard_admin_emails() -> set[str]:
    """환경변수에 등록된 운영 대시보드 관리자 이메일을 반환한다."""

    raw = os.getenv("DASHBOARD_ADMIN_EMAILS", "")
    return {value.strip().lower() for value in raw.split(",") if value.strip()}


def is_dashboard_admin(email: str | None) -> bool:
    """테스트 모드 또는 관리자 이메일 등록 여부를 확인한다."""

    if dashboard_auth_disabled():
        return True
    return bool(email and email.strip().lower() in configured_dashboard_admin_emails())
