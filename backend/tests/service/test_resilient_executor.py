import asyncio

import pytest

from service.resilience.circuit_breaker import InMemoryCircuitBreaker
from service.resilience.dead_letter import InMemoryDeadLetterStore
from service.resilience.executor import ResilientExecutor
from service.resilience.idempotency import InMemoryIdempotencyStore
from service.resilience.types import CallContext, FallbackOperation, ResiliencePolicy


def context():
    return CallContext(
        run_id="run",
        session_id="session",
        operation_id="operation",
        component="test",
        provider="primary",
    )


def test_retry_then_success_and_idempotency_replay():
    attempts = 0
    store = InMemoryIdempotencyStore()
    executor = ResilientExecutor(
        circuit_breaker=InMemoryCircuitBreaker(),
        idempotency_store=store,
    )

    async def operation():
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise TimeoutError()
        return {"ok": True}

    async def run_twice():
        first = await executor.execute(
            context=context(),
            operation=operation,
            policy=ResiliencePolicy(base_delay_ms=0),
            idempotency_key="same",
        )
        second = await executor.execute(
            context=context(),
            operation=operation,
            policy=ResiliencePolicy(base_delay_ms=0),
            idempotency_key="same",
        )
        return first, second

    first, second = asyncio.run(run_twice())
    assert first == second == {"ok": True}
    assert attempts == 2


def test_fallback_runs_after_primary_exhausts():
    executor = ResilientExecutor(circuit_breaker=InMemoryCircuitBreaker())

    async def primary():
        raise TimeoutError()

    async def fallback():
        return "fallback"

    result = asyncio.run(executor.execute(
        context=context(),
        operation=primary,
        policy=ResiliencePolicy(max_attempts=1),
        fallbacks=(FallbackOperation("secondary", fallback),),
    ))
    assert result == "fallback"


def test_exhausted_operation_enters_dlq():
    dlq = InMemoryDeadLetterStore()
    executor = ResilientExecutor(
        circuit_breaker=InMemoryCircuitBreaker(),
        dead_letter_store=dlq,
    )

    async def operation():
        raise TimeoutError("down")

    with pytest.raises(TimeoutError):
        asyncio.run(executor.execute(
            context=context(),
            operation=operation,
            policy=ResiliencePolicy(max_attempts=1),
        ))
    assert dlq.items[0]["attempts"] == 1
