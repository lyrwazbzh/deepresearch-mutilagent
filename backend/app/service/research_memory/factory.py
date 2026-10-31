"""Research-memory service factory."""

from functools import lru_cache

from .index import ResearchMemoryIndex
from .service import ResearchMemoryService


@lru_cache(maxsize=1)
def get_research_memory_service() -> ResearchMemoryService:
    return ResearchMemoryService(index=ResearchMemoryIndex.from_environment())
