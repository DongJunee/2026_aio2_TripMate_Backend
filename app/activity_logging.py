"""TripMate 사용자 행동 로그를 안전하게 저장하는 공통 함수."""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

LOGGER = logging.getLogger(__name__)


def record_activity(
    client: Any,
    *,
    user_id: str | UUID,
    event_type: str,
    trip_id: str | UUID | None = None,
    entity_type: str | None = None,
    entity_id: str | UUID | None = None,
    metadata: dict[str, Any] | None = None,
) -> None:
    """사용자 행동을 기록한다.

    로그 저장 실패가 여행 기능 자체의 실패로 이어지지 않도록 best-effort로
    동작한다. 비밀번호, 토큰, API 키, 대화 원문은 metadata에 저장하지 않는다.
    """

    values: dict[str, Any] = {
        "user_id": str(user_id),
        "event_type": event_type,
        "metadata": metadata or {},
    }
    if trip_id is not None:
        values["trip_id"] = str(trip_id)
    if entity_type:
        values["entity_type"] = entity_type
    if entity_id is not None:
        values["entity_id"] = str(entity_id)

    try:
        client.table("activity_logs").insert(values).execute()
    except Exception as error:
        LOGGER.warning("사용자 행동 로그 저장 실패 (%s).", type(error).__name__)
