"""No-op-safe Langfuse/OpenTelemetry tracing facade."""

from __future__ import annotations

import logging
import os
import sys
from contextlib import contextmanager
from threading import Thread
from typing import Any, Callable, Iterator, Mapping

try:
    from langfuse import Langfuse
except ImportError:  # metrics/logging remain usable without the optional exporter
    Langfuse = None

from .context import bind_context, current_context
from .events import sanitize_event_payload
from .logging import redact


logger = logging.getLogger(__name__)
ClientFactory = Callable[..., Any]


def _is_enabled(value: str | None, *, default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


class TraceObservation:
    """Small stable wrapper around a Langfuse observation."""

    def __init__(self, observation: Any | None, *, capture_content: bool) -> None:
        self._observation = observation
        self._capture_content = capture_content

    @property
    def enabled(self) -> bool:
        return self._observation is not None

    @property
    def trace_id(self) -> str | None:
        return str(self._observation.trace_id) if self._observation is not None else None

    @property
    def span_id(self) -> str | None:
        return str(self._observation.id) if self._observation is not None else None

    def _safe_value(self, value: Any) -> Any:
        return redact(value) if self._capture_content else sanitize_event_payload(value)

    def update(self, **fields: Any) -> None:
        if self._observation is None:
            return
        safe_fields = {
            key: self._safe_value(value) if key in {"input", "output", "metadata"} else value
            for key, value in fields.items()
        }
        try:
            self._observation.update(**safe_fields)
        except Exception:
            logger.exception("Failed to update trace observation", extra={"event": "trace.update_failed"})

    def record_usage(
        self,
        *,
        input_tokens: int,
        output_tokens: int,
        cost: float | None = None,
    ) -> None:
        usage = {
            "input": max(input_tokens, 0),
            "output": max(output_tokens, 0),
            "total": max(input_tokens, 0) + max(output_tokens, 0),
        }
        update: dict[str, Any] = {"usage_details": usage}
        if cost is not None:
            update["cost_details"] = {"total": max(cost, 0.0)}
        self.update(**update)


class TracingManager:
    """Create correlated observations without coupling application code to the SDK."""

    def __init__(self, *, client: Any | None, enabled: bool, capture_content: bool) -> None:
        self._client = client
        self.enabled = enabled and client is not None
        self.capture_content = capture_content

    @classmethod
    def disabled(cls) -> "TracingManager":
        return cls(client=None, enabled=False, capture_content=False)

    @classmethod
    def from_environment(
        cls,
        *,
        environ: Mapping[str, str] | None = None,
        client_factory: ClientFactory | None = Langfuse,
    ) -> "TracingManager":
        values = os.environ if environ is None else environ
        enabled = _is_enabled(values.get("OBSERVABILITY_TRACING_ENABLED"), default=True)
        capture_content = _is_enabled(values.get("OBSERVABILITY_CAPTURE_CONTENT"), default=False)
        public_key = values.get("LANGFUSE_PUBLIC_KEY")
        secret_key = values.get("LANGFUSE_SECRET_KEY")
        if not enabled or not public_key or not secret_key or client_factory is None:
            logger.info(
                "Tracing disabled because credentials or feature flag are absent",
                extra={"event": "trace.disabled"},
            )
            return cls.disabled()

        try:
            client = client_factory(
                public_key=public_key,
                secret_key=secret_key,
                base_url=values.get("LANGFUSE_BASE_URL", "http://localhost:3000"),
                timeout=int(values.get("LANGFUSE_TIMEOUT_SECONDS", "5")),
                tracing_enabled=True,
                environment=values.get("ENV", "development"),
                release=values.get("APP_RELEASE"),
                sample_rate=float(values.get("OTEL_TRACE_SAMPLE_RATE", "1.0")),
            )
        except Exception:
            logger.exception(
                "Failed to initialize tracing; continuing without trace export",
                extra={"event": "trace.initialization_failed"},
            )
            return cls.disabled()
        return cls(client=client, enabled=True, capture_content=capture_content)

    def _metadata(self, attributes: Mapping[str, Any] | None) -> dict[str, Any]:
        metadata = dict(attributes or {})
        context = current_context()
        if context is not None:
            metadata.update(context.as_log_fields())
        return redact(metadata)

    @contextmanager
    def span(
        self,
        name: str,
        *,
        kind: str = "span",
        attributes: Mapping[str, Any] | None = None,
    ) -> Iterator[TraceObservation]:
        if not self.enabled:
            yield TraceObservation(None, capture_content=False)
            return

        try:
            manager = self._client.start_as_current_observation(
                name=name,
                as_type=kind,
                metadata=self._metadata(attributes),
            )
            observation = manager.__enter__()
        except Exception:
            logger.exception(
                "Failed to start trace observation; using no-op span",
                extra={"event": "trace.start_failed", "details": {"name": name, "kind": kind}},
            )
            yield TraceObservation(None, capture_content=False)
            return

        wrapper = TraceObservation(observation, capture_content=self.capture_content)
        error_info = (None, None, None)
        try:
            with bind_context(trace_id=wrapper.trace_id, span_id=wrapper.span_id):
                yield wrapper
        except BaseException:
            error_info = sys.exc_info()
            wrapper.update(
                level="ERROR",
                status_message=str(error_info[1])[:500] if error_info[1] else "operation failed",
            )
            raise
        finally:
            # 这层 try/except 只照得到 manager.__exit__ 自己抛出的异常。OTel 自身
            # ContextVar 的 detach 失败在库内就被吞掉：opentelemetry/context/__init__.py
            # 的 detach() 把 _RUNTIME_CONTEXT.detach(token) 包在 except Exception 里并
            # logger.exception("Failed to detach context") —— 该 ERROR 会在 __exit__
            # 执行期间由 OTel 打出，但异常不外抛，本包裹照不到；也不要试图在这里再包
            # 一层（无效）。独立复现见 TRACKER P-20 / .runlogs/t63_otel_repro.py。
            try:
                manager.__exit__(*error_info)
            except Exception:
                logger.exception(
                    "Failed to close trace observation",
                    extra={"event": "trace.close_failed", "details": {"name": name}},
                )

    @contextmanager
    def generation(
        self,
        name: str,
        *,
        model: str,
        input_summary: Mapping[str, Any],
        attributes: Mapping[str, Any] | None = None,
    ) -> Iterator[TraceObservation]:
        safe_input = redact(input_summary) if self.capture_content else sanitize_event_payload(input_summary)
        if not self.enabled:
            yield TraceObservation(None, capture_content=False)
            return

        try:
            manager = self._client.start_as_current_observation(
                name=name,
                as_type="generation",
                model=model,
                input=safe_input,
                metadata=self._metadata(attributes),
            )
            observation = manager.__enter__()
        except Exception:
            logger.exception(
                "Failed to start generation trace; using no-op observation",
                extra={"event": "trace.generation_start_failed", "details": {"name": name}},
            )
            yield TraceObservation(None, capture_content=False)
            return

        wrapper = TraceObservation(observation, capture_content=self.capture_content)
        error_info = (None, None, None)
        try:
            with bind_context(trace_id=wrapper.trace_id, span_id=wrapper.span_id):
                yield wrapper
        except BaseException:
            error_info = sys.exc_info()
            wrapper.update(level="ERROR", status_message=str(error_info[1])[:500])
            raise
        finally:
            # 同上：OTel 自身的 detach ERROR 在库内已被 catch，本包裹照不到（P-20）。
            try:
                manager.__exit__(*error_info)
            except Exception:
                logger.exception(
                    "Failed to close generation trace",
                    extra={"event": "trace.generation_close_failed", "details": {"name": name}},
                )

    def shutdown(self, *, timeout_seconds: float = 5.0) -> bool:
        if not self.enabled:
            return True

        def flush_and_stop() -> None:
            self._client.flush()
            self._client.shutdown()

        worker = Thread(target=flush_and_stop, name="langfuse-shutdown", daemon=True)
        worker.start()
        worker.join(max(timeout_seconds, 0.0))
        if worker.is_alive():
            logger.error(
                "Tracing shutdown exceeded timeout",
                extra={"event": "trace.shutdown_timeout"},
            )
            return False
        return True


_manager = TracingManager.disabled()


def initialize_tracing() -> TracingManager:
    global _manager
    _manager = TracingManager.from_environment()
    return _manager


def shutdown_tracing(*, timeout_seconds: float = 5.0) -> bool:
    return _manager.shutdown(timeout_seconds=timeout_seconds)


@contextmanager
def span(
    name: str,
    *,
    kind: str = "span",
    attributes: Mapping[str, Any] | None = None,
) -> Iterator[TraceObservation]:
    with _manager.span(name, kind=kind, attributes=attributes) as observation:
        yield observation


@contextmanager
def generation(
    name: str,
    *,
    model: str,
    input_summary: Mapping[str, Any],
    attributes: Mapping[str, Any] | None = None,
) -> Iterator[TraceObservation]:
    with _manager.generation(
        name,
        model=model,
        input_summary=input_summary,
        attributes=attributes,
    ) as observation:
        yield observation
