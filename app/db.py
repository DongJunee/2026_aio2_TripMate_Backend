"""Supabase 클라이언트는 엔드포인트에서 필요할 때만 생성한다."""

import os
from pathlib import Path

from dotenv import load_dotenv
from supabase import Client, create_client

# backend/.env만 읽어 로컬 비밀 키가 운영체제 전체 환경 변수와 섞이지 않게 한다.
load_dotenv(Path(__file__).resolve().parents[1] / ".env")


def _setting(name: str) -> str:
    """필수 환경 변수를 읽고, 비어 있으면 설정 방법을 알리는 오류를 낸다."""
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"{name} 값을 backend/.env에 입력하세요.")
    return value


def get_anon_client() -> Client:
    """인증 및 RLS가 적용되는 일반 사용자용 Supabase 클라이언트를 만든다."""
    return create_client(_setting("SUPABASE_URL"), _setting("SUPABASE_ANON_KEY"))


def get_user_client(access_token: str) -> Client:
    """사용자 JWT를 붙여, Supabase RLS 권한이 유지된 클라이언트를 만든다."""
    client = get_anon_client()
    client.postgrest.auth(access_token)
    return client


def get_service_client() -> Client:
    """서버 내부 관리 작업에만 쓸 service-role Supabase 클라이언트를 만든다.

    API에서 소유권을 먼저 확인하지 않았다면 일반 사용자 소유 데이터에 이
    클라이언트를 사용하지 않는다. service-role 키는 RLS를 우회한다.
    """
    return create_client(
        _setting("SUPABASE_URL"), _setting("SUPABASE_SERVICE_ROLE_KEY")
    )
