"""Construct the process-wide reliability executor."""

from __future__ import annotations

from functools import lru_cache

from core.redis_client import get_redis_client

from .circuit_breaker import InMemoryCircuitBreaker, RedisCircuitBreaker
from .executor import ResilientExecutor
from .persistence import PostgresDeadLetterStore, PostgresIdempotencyStore


@lru_cache(maxsize=1)
def get_resilient_executor() -> ResilientExecutor:
    try:
        client = get_redis_client()
        client.ping()
        circuit = RedisCircuitBreaker(client)
    except Exception:
        circuit = InMemoryCircuitBreaker()
    return ResilientExecutor(
        circuit_breaker=circuit,
        idempotency_store=PostgresIdempotencyStore(),
        dead_letter_store=PostgresDeadLetterStore(),
    )
