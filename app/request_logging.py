"""API 요청을 Supabase의 api_request_logs 테이블에 기록한다."""

from __future__ import annotations

import asyncio
import logging
import os
from time import perf_counter
from uuid import UUID, uuid4

from starlette.requests import Request

from app.db import get_service_client

logger = logging.getLogger(__name__)

# Swagger 화면 자체의 요청은 운영 지표에 포함하지 않는다.
_SKIP_PATHS = {"/docs", "/redoc", "/openapi.json"}


def _request_id(request: Request) -> str:
    """클라이언트 ID를 존중하되, 없거나 공백이면 새 ID를 만든다."""
    supplied = request.headers.get("x-request-id", "").strip()
    return supplied or str(uuid4())


def _state_value(request: Request, name: str) -> str | None:
    """라우터가 선택적으로 넣어 둔 추적 정보를 안전하게 읽는다."""
    value = getattr(request.state, name, None)
    if value is None or value == "":
        return None
    return str(value)


def _trip_id_from_path(request: Request) -> str | None:
    """라우터 변경 없이 /trips/{trip_id} 경로의 여행 ID를 수집한다."""
    for part in request.url.path.split("/"):
        try:
            return str(UUID(part))
        except (ValueError, AttributeError):
            continue
    return _state_value(request, "trip_id")


def _model_from_request(request: Request) -> str | None:
    """현재 Gemini를 사용하는 API의 모델명을 기록한다."""
    state_model = _state_value(request, "model")
    if state_model:
        return state_model

    # Gemini가 설정된 여행 생성·채팅 요청은 모델 요약에 포함한다.
    if (
        os.getenv("GEMINI_API_KEY", "").strip()
        and request.method == "POST"
        and (request.url.path == "/me/trips" or request.url.path.endswith("/chat"))
    ):
        return os.getenv("GEMINI_MODEL", "").strip() or "gemini-3.5-flash-lite"
    return None


def _error_type(status_code: int | None, exception: Exception | None) -> str | None:
    if exception is not None:
        return "unhandled_exception"
    if status_code is not None and status_code >= 500:
        return "http_5xx"
    if status_code is not None and status_code >= 400:
        return "http_4xx"
    return None


def write_api_request_log(values: dict[str, object]) -> None:
    """service-role 클라이언트로 로그를 저장한다."""
    get_service_client().table("api_request_logs").insert(values).execute()


class ApiRequestLoggingMiddleware:
    """응답 본문 전송이 끝난 뒤 요청 결과를 기록하는 ASGI 미들웨어."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        request = Request(scope, receive=receive)
        request_id = _request_id(request)
        should_log = request.method != "OPTIONS" and request.url.path not in _SKIP_PATHS
        started = perf_counter()
        status_code: int | None = None
        log_written = False
        exception: Exception | None = None

        async def send_with_tracking(message):
            nonlocal status_code, log_written

            if message["type"] == "http.response.start":
                status_code = int(message["status"])
                headers = list(message.get("headers", []))
                if not any(key.lower() == b"x-request-id" for key, _ in headers):
                    headers.append((b"x-request-id", request_id.encode("ascii")))
                message = {**message, "headers": headers}

            await send(message)

            if (
                should_log
                and message["type"] == "http.response.body"
                and not message.get("more_body", False)
                and not log_written
            ):
                log_written = True
                await self._persist(
                    request=request,
                    request_id=request_id,
                    status_code=status_code or 500,
                    started=started,
                    exception=exception,
                )

        try:
            await self.app(scope, receive, send_with_tracking)
        except Exception as exc:
            exception = exc
            if should_log and not log_written:
                log_written = True
                await self._persist(
                    request=request,
                    request_id=request_id,
                    status_code=status_code or 500,
                    started=started,
                    exception=exception,
                )
            raise

    async def _persist(
        self,
        *,
        request: Request,
        request_id: str,
        status_code: int,
        started: float,
        exception: Exception | None,
    ) -> None:
        values = {
            "request_id": request_id,
            "method": request.method,
            "endpoint": request.url.path,
            "status_code": status_code,
            "latency_ms": max(0, int((perf_counter() - started) * 1000)),
            "error_type": _error_type(status_code, exception),
            "user_id": _state_value(request, "user_id"),
            "trip_id": _trip_id_from_path(request),
            "model": _model_from_request(request),
        }
        try:
            # supabase-py 호출은 동기식이므로 이벤트 루프를 막지 않는다.
            await asyncio.to_thread(write_api_request_log, values)
        except Exception:
            # 로그 저장 실패가 사용자 API 응답을 실패시키면 안 된다.
            logger.exception("API 요청 로그 저장에 실패했습니다.")
