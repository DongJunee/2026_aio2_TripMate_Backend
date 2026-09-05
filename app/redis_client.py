"""선택적으로 사용하는 Redis 연결이다. Redis 설정이 없어도 앱은 멈추지 않아야 한다."""

import os
from functools import lru_cache

import redis


@lru_cache(maxsize=1)
def get_redis() -> redis.Redis | None:
    """설정된 경우 Redis 연결 객체를 한 번 만들고 재사용한다.

    REDIS_HOST가 비어 있으면 Redis 없이도 실습 서버가 실행되도록 None을 반환한다.
    """
    host = os.getenv("REDIS_HOST", "").strip()
    if not host:
        return None

    # 포트를 입력하지 않으면 Redis 기본 포트(6379)를 사용한다.
    raw_port = os.getenv("REDIS_PORT", "").strip()
    port = int(raw_port) if raw_port else 6379
    password = os.getenv("REDIS_PASSWORD", "").strip() or None
    return redis.Redis(host=host, port=port, password=password, decode_responses=True)
