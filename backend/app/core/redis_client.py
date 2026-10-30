"""Redis client factory.

Redis is used for caching, task coordination, and transient state only. It is
never the source of truth for balances, orders, positions, fills, reservations,
or ledger entries (spec section 3.2).
"""

from __future__ import annotations

from redis import Redis

from app.core.config import Settings


def build_redis(settings: Settings) -> Redis:
    password = settings.redis_password.get_secret_value() if settings.redis_password else None
    client: Redis = Redis.from_url(
        str(settings.redis_url),
        password=password,
        socket_connect_timeout=settings.redis_timeout_seconds,
        socket_timeout=settings.redis_timeout_seconds,
        health_check_interval=30,
        decode_responses=True,
    )
    return client
