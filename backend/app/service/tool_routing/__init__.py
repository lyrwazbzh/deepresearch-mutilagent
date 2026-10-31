"""Runtime tool routing with lazy HTTP-client construction."""

from importlib import import_module

_EXPORTS = {
    "ToolRoutingService": ("service", "ToolRoutingService"),
    "get_tool_router": ("factory", "get_tool_router"),
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
