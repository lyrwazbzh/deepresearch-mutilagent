"""Retry, circuit-breaker, fallback, timeout and DLQ composition."""

from __future__ import annotations

import asyncio
import random
from typing import TypeVar

from .circuit_breaker import CircuitOpenError
from .error_classifier import is_retryable
from .timeout_budget import TimeoutBudget
from .types import CallContext, FallbackOperation, ResiliencePolicy
from observability.metrics import application_metrics


T = TypeVar("T")


class ResilientExecutor:
    def __init__(self, *, circuit_breaker, idempotency_store=None, dead_letter_store=None):
        self.circuit_breaker = circuit_breaker
        self.idempotency_store = idempotency_store
        self.dead_letter_store = dead_letter_store

    async def execute(
        self,
        *,
        context: CallContext,
        operation,
        policy: ResiliencePolicy,
        fallbacks: tuple[FallbackOperation[T], ...] = (),
        idempotency_key: str | None = None,
    ) -> T:
        if idempotency_key and self.idempotency_store:
            claim = await asyncio.to_thread(
                self.idempotency_store.claim, idempotency_key, context
            )
            if claim.completed:
                application_metrics.idempotency_hits.labels(context.component).inc()
                return claim.result
            if not claim.acquired:
                raise RuntimeError("idempotent operation is already running")

        operations = (FallbackOperation(context.provider, operation), *fallbacks)
        last_error: Exception | None = None
        total_attempts = 0
        for candidate_index, candidate in enumerate(operations):
            circuit_key = f"{candidate.name}:{context.component}"
            if not self.circuit_breaker.allow(circuit_key):
                application_metrics.resilience_circuit_rejections.labels(
                    context.component, candidate.name
                ).inc()
                last_error = CircuitOpenError(f"circuit open: {circuit_key}")
                continue
            for attempt in range(1, policy.max_attempts + 1):
                total_attempts += 1
                try:
                    timeout = TimeoutBudget(context.deadline_at).child_timeout(
                        policy.timeout_seconds
                    )
                    async with asyncio.timeout(timeout):
                        result = await candidate.call()
                    self.circuit_breaker.success(circuit_key)
                    if idempotency_key and self.idempotency_store:
                        await asyncio.to_thread(
                            self.idempotency_store.complete, idempotency_key, result
                        )
                    if candidate_index > 0:
                        application_metrics.resilience_fallbacks.labels(
                            context.component, candidate.name
                        ).inc()
                    return result
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    last_error = exc
                    retryable = is_retryable(exc)
                    self.circuit_breaker.failure(
                        circuit_key,
                        threshold=policy.circuit_failure_threshold,
                        recovery_seconds=policy.circuit_recovery_seconds,
                    )
                    if not retryable or attempt >= policy.max_attempts:
                        break
                    application_metrics.resilience_retries.labels(
                        context.component, candidate.name
                    ).inc()
                    delay_ms = min(
                        policy.base_delay_ms * (2 ** (attempt - 1)),
                        policy.max_delay_ms,
                    )
                    await asyncio.sleep(random.uniform(0, delay_ms) / 1000)

        assert last_error is not None
        if idempotency_key and self.idempotency_store:
            await asyncio.to_thread(
                self.idempotency_store.fail, idempotency_key, last_error
            )
        if policy.enqueue_dlq and self.dead_letter_store:
            await asyncio.to_thread(
                self.dead_letter_store.enqueue,
                context=context,
                error=last_error,
                attempts=total_attempts,
            )
            application_metrics.resilience_dlq.labels(context.component).inc()
        raise last_error
