"""Sanitization helpers for the durable research event ledger."""

from __future__ import annotations

import hashlib
import json
import logging
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, Mapping
from typing import Callable, Iterator

from .context import reset_context_var
from .logging import REDACTED


logger = logging.getLogger(__name__)
EventRecorder = Callable[..., Any]


@dataclass(slots=True)
class RunUsage:
    input_tokens: int = 0
    output_tokens: int = 0
    estimated_cost: float = 0.0


_EVENT_RECORDER: ContextVar[EventRecorder | None] = ContextVar("research_event_recorder", default=None)
_RUN_USAGE: ContextVar[RunUsage | None] = ContextVar("research_run_usage", default=None)


_SECRET_KEYS = {"api_key", "authorization", "cookie", "password", "secret", "token"}
_CONTENT_KEYS = {
    "content",
    "document",
    "documents",
    "final_report",
    "input",
    "output",
    "prompt",
    "query",
    "report",
    "response",
    "text",
}


def _digest(value: Any) -> str:
    serialized = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _content_summary(value: Any) -> dict[str, Any]:
    if isinstance(value, (list, tuple, set, Mapping)):
        return {"redacted": True, "count": len(value), "sha256": _digest(value)}
    rendered = str(value)
    return {"redacted": True, "length": len(rendered), "sha256": _digest(rendered)}


def sanitize_event_payload(value: Any, *, max_text_length: int = 512) -> Any:
    """Keep diagnostic metadata while excluding secrets and content-heavy fields."""

    if isinstance(value, Mapping):
        sanitized: dict[str, Any] = {}
        for raw_key, item in value.items():
            key = str(raw_key)
            normalized_key = key.strip().lower()
            if normalized_key in _SECRET_KEYS or normalized_key.endswith(("_secret", "_password", "_api_key")):
                sanitized[key] = REDACTED
            elif normalized_key in _CONTENT_KEYS:
                sanitized[key] = _content_summary(item)
            else:
                sanitized[key] = sanitize_event_payload(item, max_text_length=max_text_length)
        return sanitized
    if isinstance(value, (list, tuple, set)):
        return [sanitize_event_payload(item, max_text_length=max_text_length) for item in value[:100]]
    if isinstance(value, str) and len(value) > max_text_length:
        return f"{value[:max_text_length]}...[TRUNCATED]"
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


@contextmanager
def bind_event_recorder(recorder: EventRecorder) -> Iterator[None]:
    token = _EVENT_RECORDER.set(recorder)
    try:
        yield
    finally:
        reset_context_var(_EVENT_RECORDER, token)


def record_research_event(
    event_type: str,
    *,
    phase: str | None = None,
    status: str = "info",
    payload: dict[str, Any] | None = None,
    duration_ms: int | None = None,
) -> None:
    """Record an event when a run ledger is bound; never affect the workload."""

    recorder = _EVENT_RECORDER.get()
    if recorder is None:
        return
    try:
        recorder(
            event_type=event_type,
            phase=phase,
            status=status,
            payload=payload or {},
            duration_ms=duration_ms,
        )
    except Exception:
        logger.exception(
            "Bound event recorder failed",
            extra={"event": "observability.event_recorder_failed", "details": {"event_type": event_type}},
        )


@contextmanager
def bind_run_usage() -> Iterator[RunUsage]:
    usage = RunUsage()
    token = _RUN_USAGE.set(usage)
    try:
        yield usage
    finally:
        reset_context_var(_RUN_USAGE, token)


def add_run_usage(*, input_tokens: int, output_tokens: int, cost: float = 0.0) -> None:
    usage = _RUN_USAGE.get()
    if usage is None:
        return
    usage.input_tokens += max(input_tokens, 0)
    usage.output_tokens += max(output_tokens, 0)
    usage.estimated_cost = round(usage.estimated_cost + max(cost, 0.0), 6)
