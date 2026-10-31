"""Correlation context shared by logs, metrics, traces, and research events."""

from __future__ import annotations

import logging
import re
from contextlib import contextmanager
from contextvars import ContextVar, Token
from dataclasses import asdict, dataclass, replace
from typing import Iterator
from uuid import uuid4


logger = logging.getLogger(__name__)


_CORRELATION_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")


@dataclass(frozen=True, slots=True)
class ObservabilityContext:
    """Identifiers that connect one request to its research execution."""

    request_id: str
    session_id: str | None = None
    research_id: str | None = None
    run_id: str | None = None
    trace_id: str | None = None
    span_id: str | None = None

    def as_log_fields(self) -> dict[str, str]:
        """Return only populated identifiers, suitable for structured logs."""

        return {key: value for key, value in asdict(self).items() if value is not None}


_CURRENT_CONTEXT: ContextVar[ObservabilityContext | None] = ContextVar(
    "observability_context",
    default=None,
)


def reset_context_var(var: ContextVar, token: Token) -> None:
    """Reset ``var`` only when ``token`` belongs to the current Context.

    Async generators may be closed from a task whose Context differs from the one
    that entered the context manager —— SSE 客户端断连时 Starlette 正是这样把响应
    生成器 teardown 掉的。此时 ``ContextVar.reset`` 抛
    ``ValueError("… was created in a different Context")``，把干净的 ``GeneratorExit``
    掩盖成一个 context 错误（T62）。

    当前 Context 从未执行过对应的 ``set``，因此这里**没有需要撤销的东西** ——
    跳过重置在语义上是正确的，不是「吞掉真实错误」。

    **只放行「跨 Context」这一种 `ValueError`**：`ContextVar.reset` 的
    `ValueError` 有两类成因，措辞不同 ——

    * ``… was created in a different Context`` —— 本函数要容忍的场景，跳过；
    * ``… was created by a different ContextVar`` —— **拿错 var 的编程错误**，
      必须原样抛出，不能被守卫吞掉。

    判别子串取 ``"in a different Context"`` 而非 ``"different Context"``：
    后者是前者的**前缀**，而 ``"different ContextVar"`` 里同样含
    ``"different Context"``（Python 3.11 / 3.13 实测均如此），用短串会把
    「拿错 var」一并吞掉。
    """

    try:
        var.reset(token)
    except ValueError as exc:
        if "in a different Context" not in str(exc):
            raise
        logger.debug(
            "Skipped ContextVar reset: token belongs to a different Context",
            extra={
                "event": "observability.context_reset_skipped",
                "details": {"var": var.name},
            },
        )


def normalize_correlation_id(value: str | None, *, prefix: str = "req") -> str:
    """Accept a safe external identifier or generate an opaque replacement."""

    if value and _CORRELATION_ID_PATTERN.fullmatch(value):
        return value
    return f"{prefix}-{uuid4().hex}"


def new_request_context(
    *,
    request_id: str | None = None,
    session_id: str | None = None,
    research_id: str | None = None,
) -> ObservabilityContext:
    """Create the root context for an incoming HTTP request."""

    return ObservabilityContext(
        request_id=normalize_correlation_id(request_id),
        session_id=session_id,
        research_id=research_id,
    )


def current_context() -> ObservabilityContext | None:
    """Return the context bound to the current async task, if any."""

    return _CURRENT_CONTEXT.get()


@contextmanager
def bind_context(
    context: ObservabilityContext | None = None,
    **updates: str | None,
) -> Iterator[ObservabilityContext]:
    """Temporarily bind a context and restore its parent on exit."""

    parent = current_context()
    if context is None:
        context = parent or new_request_context()
    if updates:
        unknown = set(updates) - set(ObservabilityContext.__dataclass_fields__)
        if unknown:
            raise TypeError(f"Unknown observability context fields: {sorted(unknown)}")
        context = replace(context, **updates)

    token = _CURRENT_CONTEXT.set(context)
    try:
        yield context
    finally:
        reset_context_var(_CURRENT_CONTEXT, token)

