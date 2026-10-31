"""Shared reliability primitives with lazy infrastructure adapters."""

from importlib import import_module

_EXPORTS = {
    "CallContext": ("types", "CallContext"),
    "FallbackOperation": ("types", "FallbackOperation"),
    "ResiliencePolicy": ("types", "ResiliencePolicy"),
    "ResilientExecutor": ("executor", "ResilientExecutor"),
    "get_resilient_executor": ("factory", "get_resilient_executor"),
}
__all__ = list(_EXPORTS)


def __getattr__(name):
    try:
        module, attribute = _EXPORTS[name]
    except KeyError as exc:
        raise AttributeError(name) from exc
    value = getattr(import_module(f"{__name__}.{module}"), attribute)
    globals()[name] = value
    return value
