"""Fact/Experience research memory with lazy infrastructure adapters."""

from importlib import import_module

_EXPORTS = {
    "ExperienceMemoryInput": ("types", "ExperienceMemoryInput"),
    "FactMemoryInput": ("types", "FactMemoryInput"),
    "MemoryHit": ("types", "MemoryHit"),
    "MemoryQuery": ("types", "MemoryQuery"),
    "ResearchMemoryService": ("service", "ResearchMemoryService"),
    "get_research_memory_service": ("factory", "get_research_memory_service"),
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
