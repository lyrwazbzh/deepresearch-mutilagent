"""Typed policy and call metadata for resilient execution."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Generic, TypeVar


T = TypeVar("T")


@dataclass(frozen=True)
class CallContext:
    run_id: str
    session_id: str
    operation_id: str
    component: str
    provider: str
    user_id: str | None = None
    deadline_at: datetime | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ResiliencePolicy:
    max_attempts: int = 3
    base_delay_ms: int = 300
    max_delay_ms: int = 3000
    timeout_seconds: float = 30.0
    circuit_failure_threshold: int = 5
    circuit_recovery_seconds: int = 60
    enqueue_dlq: bool = True


@dataclass(frozen=True)
class FallbackOperation(Generic[T]):
    name: str
    call: Callable[[], Awaitable[T]]
