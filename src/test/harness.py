from __future__ import annotations

import random
from collections import defaultdict, deque
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.sdk.trace.id_generator import IdGenerator

from src.agent.schema.model_config import ModelConfig
from src.agent.llm.runtime import ProviderChatResponse
from src.agent.backends.observability import Telemetry
from src.agent.backends.persistence.database import SessionFactory


@dataclass
class TestClock:
    __test__ = False
    current: datetime = datetime(2025, 1, 1, tzinfo=timezone.utc)

    def now(self) -> datetime:
        return self.current

    def advance(self, **delta: float) -> datetime:
        self.current += timedelta(**delta)
        return self.current


class DeterministicIdFactory:
    def __init__(self, namespace: str = "test") -> None:
        self.namespace = namespace
        self._counters: dict[str, int] = defaultdict(int)

    def __call__(self, prefix: str) -> str:
        self._counters[prefix] += 1
        return f"{prefix}_{self.namespace}_{self._counters[prefix]:04d}"


@contextmanager
def FixedRandomSeed(seed: int) -> Iterator[random.Random]:
    state = random.getstate()
    random.seed(seed)
    generator = random.Random(seed)
    try:
        yield generator
    finally:
        random.setstate(state)


class ProviderAttemptSequence:
    """Fake only the external ProviderClient boundary, never ModelRuntime."""

    def __init__(
        self,
        *,
        chat: Iterable[Any] = (),
        embed: Iterable[Any] = (),
        rerank: Iterable[Any] = (),
    ) -> None:
        self._values = {
            "chat": deque(chat),
            "embed": deque(embed),
            "rerank": deque(rerank),
        }
        self.attempts: list[tuple[str, str, str]] = []

    def _next(self, operation: str, config: ModelConfig) -> Any:
        self.attempts.append((operation, config.provider, config.model))
        if not self._values[operation]:
            raise AssertionError(f"No scripted Provider attempt remains for {operation}")
        value = self._values[operation].popleft()
        if isinstance(value, BaseException):
            raise value
        return value

    def chat(self, config: ModelConfig, prompt: str) -> str | ProviderChatResponse:
        del prompt
        return self._next("chat", config)

    def embed(self, config: ModelConfig, texts: list[str]) -> list[list[float]]:
        del texts
        return self._next("embed", config)

    def rerank(
        self, config: ModelConfig, query: str, candidates: list[str]
    ) -> list[float]:
        del query, candidates
        return self._next("rerank", config)


class DeterministicTraceIdGenerator(IdGenerator):
    def __init__(self) -> None:
        self._trace = 0
        self._span = 0

    def generate_trace_id(self) -> int:
        self._trace += 1
        return self._trace

    def generate_span_id(self) -> int:
        self._span += 1
        return self._span


def in_memory_telemetry(
    session_factory: SessionFactory,
) -> tuple[Telemetry, InMemorySpanExporter]:
    exporter = InMemorySpanExporter()
    telemetry = Telemetry(
        session_factory,
        exporter=exporter,
        synchronous=True,
        id_generator=DeterministicTraceIdGenerator(),
    )
    return telemetry, exporter
