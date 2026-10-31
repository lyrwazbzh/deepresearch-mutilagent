"""Reusable instrumentation decorators for retrieval and tool boundaries."""

from __future__ import annotations

from functools import wraps
from time import perf_counter
from typing import Awaitable, Callable, ParamSpec, TypeVar

from .events import record_research_event
from .metrics import ApplicationMetrics, application_metrics
from .tracing import span


P = ParamSpec("P")
R = TypeVar("R")


def _result_outcome(result: object) -> str:
    if isinstance(result, dict) and result.get("success") is False:
        return "failure"
    if result is None or result == []:
        return "empty"
    return "success"


def _result_summary(result: object) -> dict[str, object]:
    if isinstance(result, (list, tuple, set)):
        return {"result_count": len(result)}
    if isinstance(result, dict):
        return {
            "success": result.get("success", True),
            "result_count": len(result.get("results", [])) if isinstance(result.get("results"), list) else None,
            "chart_count": len(result.get("charts", [])) if isinstance(result.get("charts"), list) else None,
        }
    return {"result_type": type(result).__name__}


def observe_retrieval(
    source: str,
    *,
    metrics: ApplicationMetrics = application_metrics,
) -> Callable[[Callable[P, Awaitable[R]]], Callable[P, Awaitable[R]]]:
    """Trace and measure an async retrieval boundary."""

    def decorator(function: Callable[P, Awaitable[R]]) -> Callable[P, Awaitable[R]]:
        @wraps(function)
        async def wrapped(*args: P.args, **kwargs: P.kwargs) -> R:
            started_at = perf_counter()
            try:
                with span(f"retrieval.{source}", kind="retriever", attributes={"source": source}) as observation:
                    result = await function(*args, **kwargs)
                    outcome = _result_outcome(result)
                    summary = _result_summary(result)
                    observation.update(output=summary)
            except Exception as exc:
                duration = perf_counter() - started_at
                metrics.retrieval_duration.labels(source, "failure").observe(duration)
                record_research_event(
                    "retrieval.failed",
                    status="error",
                    payload={"source": source, "error_type": type(exc).__name__},
                    duration_ms=round(duration * 1000),
                )
                raise

            duration = perf_counter() - started_at
            metrics.retrieval_duration.labels(source, outcome).observe(duration)
            record_research_event(
                "retrieval.completed",
                status="warning" if outcome == "empty" else "info",
                payload={"source": source, "outcome": outcome, **summary},
                duration_ms=round(duration * 1000),
            )
            return result

        return wrapped

    return decorator


def observe_tool(
    tool: str,
    *,
    metrics: ApplicationMetrics = application_metrics,
) -> Callable[[Callable[P, Awaitable[R]]], Callable[P, Awaitable[R]]]:
    """Trace and measure an async research tool boundary."""

    def decorator(function: Callable[P, Awaitable[R]]) -> Callable[P, Awaitable[R]]:
        @wraps(function)
        async def wrapped(*args: P.args, **kwargs: P.kwargs) -> R:
            started_at = perf_counter()
            try:
                with span(f"tool.{tool}", kind="tool", attributes={"tool": tool}) as observation:
                    result = await function(*args, **kwargs)
                    outcome = _result_outcome(result)
                    summary = _result_summary(result)
                    observation.update(output=summary)
            except Exception as exc:
                duration = perf_counter() - started_at
                metrics.tool_calls.labels(tool, "failure").inc()
                metrics.tool_duration.labels(tool, "failure").observe(duration)
                record_research_event(
                    "tool.failed",
                    status="error",
                    payload={"tool": tool, "error_type": type(exc).__name__},
                    duration_ms=round(duration * 1000),
                )
                raise

            duration = perf_counter() - started_at
            metrics.tool_calls.labels(tool, outcome).inc()
            metrics.tool_duration.labels(tool, outcome).observe(duration)
            record_research_event(
                "tool.completed" if outcome == "success" else "tool.failed",
                status="info" if outcome == "success" else "error",
                payload={"tool": tool, "outcome": outcome, **summary},
                duration_ms=round(duration * 1000),
            )
            return result

        return wrapped

    return decorator

