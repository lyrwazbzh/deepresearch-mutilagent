"""Public observability primitives, loaded without eager optional exporters."""

from __future__ import annotations

from importlib import import_module


_EXPORTS = {
    "ObservabilityContext": ("context", "ObservabilityContext"),
    "bind_context": ("context", "bind_context"),
    "current_context": ("context", "current_context"),
    "new_request_context": ("context", "new_request_context"),
    "normalize_correlation_id": ("context", "normalize_correlation_id"),
    "JsonFormatter": ("logging", "JsonFormatter"),
    "configure_logging": ("logging", "configure_logging"),
    "redact": ("logging", "redact"),
    "ApplicationMetrics": ("metrics", "ApplicationMetrics"),
    "application_metrics": ("metrics", "application_metrics"),
    "metrics_response": ("metrics", "metrics_response"),
    "ObservabilityMiddleware": ("middleware", "ObservabilityMiddleware"),
    "TraceObservation": ("tracing", "TraceObservation"),
    "TracingManager": ("tracing", "TracingManager"),
    "generation": ("tracing", "generation"),
    "initialize_tracing": ("tracing", "initialize_tracing"),
    "shutdown_tracing": ("tracing", "shutdown_tracing"),
    "span": ("tracing", "span"),
}

__all__ = list(_EXPORTS)


def __getattr__(name: str):
    try:
        module_name, attribute = _EXPORTS[name]
    except KeyError as exc:
        raise AttributeError(name) from exc
    value = getattr(import_module(f"{__name__}.{module_name}"), attribute)
    globals()[name] = value
    return value
