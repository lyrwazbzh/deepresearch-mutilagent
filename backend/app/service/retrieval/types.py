"""Backend-neutral retrieval types."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass(frozen=True)
class RetrievalRequest:
    query: str
    kb_name: str
    top_k: int = 10
    user_id: str | None = None
    filters: dict[str, Any] = field(default_factory=dict)


@dataclass
class RetrievalHit:
    chunk_id: str
    document_id: str
    content: str
    filename: str
    source: str
    score: float
    rank: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_legacy_dict(self) -> dict[str, Any]:
        """Return the shape consumed by DeepScout and the existing UI."""

        return {
            "id": self.chunk_id,
            "doc_id": self.document_id,
            "title": self.filename or "本地知识库文档",
            "filename": self.filename,
            "content": self.content,
            "summary": self.content,
            "snippet": self.content,
            "url": "",
            "source": "local_knowledge_base",
            "site_name": "本地知识库",
            "score": self.score,
            "retrieval_source": self.source,
            "metadata": self.metadata,
        }


class Retriever(Protocol):
    async def search(self, request: RetrievalRequest) -> list[RetrievalHit]: ...
