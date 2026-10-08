from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from threading import Lock
from typing import Any, Literal

import httpx
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import (
    BatchSpanProcessor,
    SimpleSpanProcessor,
    SpanExporter,
    SpanExportResult,
)
from opentelemetry.sdk.trace.id_generator import IdGenerator
from opentelemetry.sdk.trace.sampling import ALWAYS_ON
from opentelemetry.trace import SpanKind, Status, StatusCode
from sqlalchemy import delete, select

from src.agent.backends.persistence.database import SessionFactory
from src.agent.backends.persistence.models import ObservabilitySpanRow


ErrorCategory = Literal["provider", "infrastructure", "model_semantic"]

_SCOPE_TENANT = "_scope.tenant_id"
_SCOPE_USER = "_scope.user_id"
_SCOPE_TASK = "_scope.task_id"
_MAX_ATTRIBUTES = 48
_MAX_EVENTS = 32
_MAX_EVENT_ATTRIBUTES = 8
_MAX_STRING = 256
_SAFE_PREFIXES = ("research.", "gen_ai.", "retrieval.", "memory.", "app.cost.")
_SAFE_KEYS = frozenset(
    {
        "tool.name",
        "skill.name",
        "error.type",
        "error.category",
        "client.operation",
        "server.address",
        "service.name",
        "telemetry.sdk.language",
        "telemetry.sdk.name",
        "telemetry.sdk.version",
    }
)
_SAFE_EVENTS: dict[str, frozenset[str]] = {
    "tool.retry": frozenset({"attempt_no", "provider", "error.type"}),
    "tool.fallback": frozenset({"from_provider", "to_provider", "reason_code"}),
    "retrieval.degraded": frozenset({"reason_code"}),
    "memory.degraded": frozenset({"reason_code"}),
    "citation.validation_failed": frozenset(
        {"validation.code", "claim_id", "citation_id", "evidence_id"}
    ),
    "circuit_breaker.rejected": frozenset({"provider", "operation"}),
    "circuit_breaker.state_changed": frozenset(
        {"provider", "operation", "from", "to"}
    ),
}


@dataclass(frozen=True)
class TraceScope:
    tenant_id: str
    user_id: str | None = None
    task_id: str | None = None


_current_scope: ContextVar[TraceScope | None] = ContextVar(
    "deepresearch_trace_scope", default=None
)
_current_telemetry: ContextVar["Telemetry | None"] = ContextVar(
    "deepresearch_telemetry", default=None
)
_event_target: ContextVar[Any | None] = ContextVar(
    "deepresearch_span_event_target", default=None
)


def classify_error(exc: BaseException, *, semantic: bool = False) -> ErrorCategory:
    if semantic or getattr(exc, "error_code", None) in {
        "malformed_output",
        "schema_validation",
        "semantic_validation",
    }:
        return "model_semantic"
    if isinstance(exc, httpx.HTTPError) or getattr(exc, "error_code", "").startswith(
        "provider_"
    ):
        return "provider"
    return "infrastructure"


def _safe_value(value: Any) -> str | bool | int | float | list[str] | None:
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (list, tuple)):
        return [str(item)[:_MAX_STRING] for item in value[:16]]
    return str(value)[:_MAX_STRING]


def bounded_attributes(attributes: Mapping[str, Any] | None) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in (attributes or {}).items():
        if key in {_SCOPE_TENANT, _SCOPE_USER, _SCOPE_TASK}:
            continue
        if key not in _SAFE_KEYS and not key.startswith(_SAFE_PREFIXES):
            continue
        safe = _safe_value(value)
        if safe is not None:
            result[key] = safe
        if len(result) >= _MAX_ATTRIBUTES:
            break
    return result


class MetricRegistry:
    """Small process-local metric facade with frozen, low-cardinality labels."""

    LABELS: dict[str, tuple[str, ...]] = {
        "memory_operation_duration_seconds": ("operation", "status"),
        "memory_recall_degraded_total": ("reason_code",),
        "memory_embedding_pending_oldest_seconds": (),
        "memory_pending_conflicts": (),
        "memory_stale_conflict_total": (),
        "circuit_breaker_transition_total": ("provider", "operation", "from", "to"),
        "circuit_breaker_rejected_total": ("provider", "operation"),
        "telemetry_export_failed_total": (),
        "telemetry_dropped_spans_total": (),
        "semantic_cache_lookup_total": ("operation", "result"),
    }
    _FORBIDDEN_LABELS = frozenset(
        {"tenant_id", "user_id", "task_id", "query", "url", "resource_id"}
    )

    def __init__(self) -> None:
        self._lock = Lock()
        self._values: dict[tuple[str, tuple[tuple[str, str], ...]], float] = defaultdict(float)
        self._counts: dict[tuple[str, tuple[tuple[str, str], ...]], int] = defaultdict(int)

    def add(self, name: str, value: float = 1.0, **labels: str) -> None:
        expected = self.LABELS.get(name)
        if expected is None:
            raise KeyError(f"Unknown Phase 12 metric: {name}")
        if set(labels) & self._FORBIDDEN_LABELS or set(labels) != set(expected):
            raise ValueError(f"Invalid labels for {name}: {sorted(labels)}")
        key = (name, tuple(sorted((item, str(labels[item])[:64]) for item in expected)))
        with self._lock:
            self._values[key] += float(value)
            if name == "memory_operation_duration_seconds":
                self._counts[key] += 1

    def set(self, name: str, value: float, **labels: str) -> None:
        expected = self.LABELS.get(name)
        if expected is None or set(labels) != set(expected):
            raise ValueError(f"Invalid labels for {name}")
        key = (name, tuple(sorted((item, str(labels[item])[:64]) for item in expected)))
        with self._lock:
            self._values[key] = float(value)

    def snapshot(self) -> dict[str, float]:
        with self._lock:
            values = {
                name + "{" + ",".join(f"{key}={value}" for key, value in labels) + "}": amount
                for (name, labels), amount in self._values.items()
            }
            values.update(
                {
                    name
                    + "_count{"
                    + ",".join(f"{key}={value}" for key, value in labels)
                    + "}": float(amount)
                    for (name, labels), amount in self._counts.items()
                }
            )
            return values


class PostgresSpanExporter(SpanExporter):
    """Best-effort batch exporter for the frozen observability_spans table."""

    def __init__(self, session_factory: SessionFactory, metrics: MetricRegistry) -> None:
        self.session_factory = session_factory
        self.metrics = metrics

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        try:
            rows = [self._row(span) for span in spans]
            with self.session_factory() as session, session.begin():
                session.add_all(rows)
            return SpanExportResult.SUCCESS
        except Exception:
            self.metrics.add("telemetry_export_failed_total")
            return SpanExportResult.FAILURE

    @staticmethod
    def _row(span: ReadableSpan) -> ObservabilitySpanRow:
        attributes = dict(span.attributes or {})
        tenant_id = str(attributes.pop(_SCOPE_TENANT, "system"))[:64]
        user = attributes.pop(_SCOPE_USER, None)
        task = attributes.pop(_SCOPE_TASK, None)
        resource = {
            key: value
            for key, value in dict(span.resource.attributes).items()
            if key in _SAFE_KEYS
        }
        safe_attributes = bounded_attributes({**resource, **attributes})
        events: list[dict[str, Any]] = []
        for event in span.events[:_MAX_EVENTS]:
            allowed = _SAFE_EVENTS.get(event.name)
            if allowed is None:
                continue
            event_attrs = {
                key: _safe_value(value)
                for key, value in dict(event.attributes or {}).items()
                if key in allowed
            }
            events.append(
                {
                    "name": event.name,
                    "timestamp": datetime.fromtimestamp(
                        event.timestamp / 1_000_000_000, tz=timezone.utc
                    ).isoformat(),
                    "attributes": dict(list(event_attrs.items())[:_MAX_EVENT_ATTRIBUTES]),
                }
            )
        context = span.context
        if span.start_time is None or span.end_time is None:
            raise ValueError("Only ended spans can be exported")
        parent_span_id = f"{span.parent.span_id:016x}" if span.parent else None
        return ObservabilitySpanRow(
            tenant_id=tenant_id,
            user_id=str(user)[:64] if user else None,
            task_id=str(task)[:64] if task else None,
            trace_id=f"{context.trace_id:032x}",
            span_id=f"{context.span_id:016x}",
            parent_span_id=parent_span_id,
            name=span.name[:128],
            kind=span.kind.name,
            started_at=datetime.fromtimestamp(span.start_time / 1_000_000_000, tz=timezone.utc),
            ended_at=datetime.fromtimestamp(span.end_time / 1_000_000_000, tz=timezone.utc),
            status_code=span.status.status_code.name,
            attributes=safe_attributes,
            events=events,
        )


class CountingBatchSpanProcessor(BatchSpanProcessor):
    def __init__(self, exporter: SpanExporter, metrics: MetricRegistry, **kwargs: Any) -> None:
        self._phase12_metrics = metrics
        super().__init__(exporter, **kwargs)

    def on_end(self, span: ReadableSpan) -> None:
        queue = getattr(self, "queue", None)
        maximum = getattr(self, "max_queue_size", None)
        if queue is not None and maximum is not None and len(queue) >= maximum:
            self._phase12_metrics.add("telemetry_dropped_spans_total")
        super().on_end(span)


class Telemetry:
    def __init__(
        self,
        session_factory: SessionFactory,
        *,
        exporter: SpanExporter | None = None,
        metrics: MetricRegistry | None = None,
        synchronous: bool = False,
        id_generator: IdGenerator | None = None,
        max_queue_size: int = 2048,
        max_export_batch_size: int = 256,
        schedule_delay_millis: int = 1_000,
    ) -> None:
        self.metrics = metrics or MetricRegistry()
        self.exporter = exporter or PostgresSpanExporter(session_factory, self.metrics)
        self.provider = TracerProvider(
            sampler=ALWAYS_ON,
            resource=Resource.create({"service.name": "deepresearch-mutilagent"}),
            id_generator=id_generator,
        )
        if synchronous:
            self.processor: Any = SimpleSpanProcessor(self.exporter)
        else:
            self.processor = CountingBatchSpanProcessor(
                self.exporter,
                self.metrics,
                max_queue_size=max_queue_size,
                max_export_batch_size=max_export_batch_size,
                schedule_delay_millis=schedule_delay_millis,
            )
        self.provider.add_span_processor(self.processor)
        self.tracer = self.provider.get_tracer("deepresearch", "phase12")

    @contextmanager
    def span(
        self,
        name: str,
        *,
        scope: TraceScope | None = None,
        kind: SpanKind = SpanKind.INTERNAL,
        attributes: Mapping[str, Any] | None = None,
        semantic_errors: tuple[type[BaseException], ...] = (),
    ) -> Iterator[Any]:
        parent_scope = _current_scope.get()
        resolved_scope = scope or parent_scope
        if (
            scope is not None
            and parent_scope is not None
            and scope.tenant_id == parent_scope.tenant_id
        ):
            resolved_scope = TraceScope(
                tenant_id=scope.tenant_id,
                user_id=scope.user_id or parent_scope.user_id,
                task_id=scope.task_id or parent_scope.task_id,
            )
        values: dict[str, Any] = dict(attributes or {})
        if resolved_scope is not None:
            values[_SCOPE_TENANT] = resolved_scope.tenant_id
            if resolved_scope.user_id:
                values[_SCOPE_USER] = resolved_scope.user_id
            if resolved_scope.task_id:
                values[_SCOPE_TASK] = resolved_scope.task_id
        scope_token = _current_scope.set(resolved_scope)
        telemetry_token = _current_telemetry.set(self)
        try:
            with self.tracer.start_as_current_span(name, kind=kind, attributes=values) as span:
                try:
                    yield span
                    span.set_status(Status(StatusCode.OK))
                except BaseException as exc:
                    span.set_status(Status(StatusCode.ERROR))
                    span.set_attribute("error.type", getattr(exc, "error_code", type(exc).__name__))
                    span.set_attribute(
                        "error.category",
                        classify_error(exc, semantic=isinstance(exc, semantic_errors)),
                    )
                    raise
        finally:
            _current_telemetry.reset(telemetry_token)
            _current_scope.reset(scope_token)

    def force_flush(self, timeout_millis: int = 10_000) -> bool:
        return bool(self.provider.force_flush(timeout_millis))

    def shutdown(self) -> None:
        self.provider.shutdown()


@contextmanager
def current_span(
    name: str,
    *,
    kind: SpanKind = SpanKind.INTERNAL,
    attributes: Mapping[str, Any] | None = None,
    semantic_errors: tuple[type[BaseException], ...] = (),
) -> Iterator[Any | None]:
    telemetry = _current_telemetry.get()
    if telemetry is None:
        yield None
        return
    with telemetry.span(
        name, kind=kind, attributes=attributes, semantic_errors=semantic_errors
    ) as span:
        yield span


def add_span_event(name: str, attributes: Mapping[str, Any] | None = None) -> None:
    from opentelemetry import trace

    allowed = _SAFE_EVENTS.get(name)
    if allowed is None:
        return
    span = _event_target.get() or trace.get_current_span()
    if not span.is_recording():
        return
    safe = {
        key: value
        for key, value in (attributes or {}).items()
        if key in allowed
    }
    span.add_event(name, safe)


@contextmanager
def bind_span_event_target(span: Any | None) -> Iterator[None]:
    token = _event_target.set(span)
    try:
        yield
    finally:
        _event_target.reset(token)


class CostQueryService:
    DIMENSIONS = {
        "tenant": None,
        "task": None,
        "agent": "research.agent",
        "provider": "gen_ai.provider.name",
        "model": "gen_ai.request.model",
        "operation": "gen_ai.operation.name",
        "currency": "app.cost.currency",
    }

    def __init__(self, session_factory: SessionFactory) -> None:
        self.session_factory = session_factory

    def aggregate(
        self,
        tenant_id: str,
        *,
        dimensions: tuple[str, ...] = ("currency",),
        task_id: str | None = None,
        started_at: datetime | None = None,
        ended_at: datetime | None = None,
    ) -> list[dict[str, Any]]:
        if any(item not in self.DIMENSIONS for item in dimensions):
            raise ValueError("Unsupported cost aggregation dimension")
        statement = select(ObservabilitySpanRow).where(
            ObservabilitySpanRow.tenant_id == tenant_id,
            ObservabilitySpanRow.name.like("gen_ai.%"),
        )
        if task_id:
            statement = statement.where(ObservabilitySpanRow.task_id == task_id)
        if started_at:
            statement = statement.where(ObservabilitySpanRow.started_at >= started_at)
        if ended_at:
            statement = statement.where(ObservabilitySpanRow.started_at < ended_at)
        totals: dict[tuple[Any, ...], Decimal] = defaultdict(Decimal)
        with self.session_factory() as session:
            for row in session.scalars(statement):
                attributes = dict(row.attributes)
                amount = Decimal(str(attributes.get("app.cost.amount") or "0"))
                values: list[Any] = []
                for dimension in dimensions:
                    if dimension == "tenant":
                        values.append(row.tenant_id)
                    elif dimension == "task":
                        values.append(row.task_id)
                    else:
                        values.append(attributes.get(self.DIMENSIONS[dimension] or ""))
                totals[tuple(values)] += amount
        return [
            {
                **dict(zip(dimensions, key, strict=True)),
                "amount": str(amount),
            }
            for key, amount in sorted(totals.items(), key=lambda item: repr(item[0]))
        ]


class TraceRetentionService:
    def __init__(self, session_factory: SessionFactory) -> None:
        self.session_factory = session_factory

    def purge_before(self, cutoff: datetime) -> int:
        with self.session_factory() as session, session.begin():
            result = session.execute(
                delete(ObservabilitySpanRow).where(
                    ObservabilitySpanRow.started_at < cutoff
                )
            )
            return int(result.rowcount or 0)
