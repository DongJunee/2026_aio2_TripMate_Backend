"""TripMate 운영 대시보드 권한 판별을 위한 공통 함수."""

from __future__ import annotations

from typing import Any


def is_dashboard_admin(profile: dict[str, Any] | None) -> bool:
    """Supabase ``profiles.is_admin`` 값으로 관리자 여부를 판별한다."""

    return bool(profile and profile.get("is_admin") is True)
