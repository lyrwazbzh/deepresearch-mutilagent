"""Hybrid knowledge retrieval with lazy optional backend imports."""

from importlib import import_module

_EXPORTS = {
    "RetrievalHit": ("types", "RetrievalHit"),
    "RetrievalRequest": ("types", "RetrievalRequest"),
    "Retriever": ("types", "Retriever"),
    "get_hybrid_retriever": ("factory", "get_hybrid_retriever"),
    "get_knowledge_index_service": ("factory", "get_knowledge_index_service"),
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
