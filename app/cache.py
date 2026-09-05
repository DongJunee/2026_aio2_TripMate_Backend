"""최선의 노력으로 동작하는 캐시 도우미이다. 실제 기준 데이터는 Supabase에 둔다."""

import logging

from redis.exceptions import RedisError

from app.redis_client import get_redis

logger = logging.getLogger(__name__)


def cache_get(key: str) -> str | None:
    """Redis에서 캐시 값을 읽고, 연결 실패 시에는 캐시 없이 계속 진행한다."""
    client = get_redis()
    if client is None:
        return None
    try:
        return client.get(key)
    except RedisError as error:
        logger.warning("Redis read failed: %s", error)
        return None


def cache_set(key: str, value: str, ttl_seconds: int) -> None:
    """Redis에 만료 시간이 있는 문자열 캐시 값을 저장한다."""
    client = get_redis()
    if client is None:
        return
    try:
        client.set(key, value, ex=ttl_seconds)
    except RedisError as error:
        logger.warning("Redis write failed: %s", error)


def cache_delete(key: str) -> None:
    """Redis에서 지정한 캐시 키를 삭제한다."""
    client = get_redis()
    if client is None:
        return
    try:
        client.delete(key)
    except RedisError as error:
        logger.warning("Redis delete failed: %s", error)
