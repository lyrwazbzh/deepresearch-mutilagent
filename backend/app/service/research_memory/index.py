"""Milvus + Elasticsearch index used by Fact/Experience memories."""

from __future__ import annotations

import asyncio
import logging
import os
from collections import defaultdict
from typing import Any

from service.embedding_service import generate_embedding
from service.milvus_service import get_milvus_service


MEMORY_COLLECTION = "research_memories_v1"
MEMORY_INDEX = "research_memories_v1"
logger = logging.getLogger(__name__)

MEMORY_INDEX_MAPPING = {
    "mappings": {"properties": {
        "memory_id": {"type": "keyword"},
        "user_id": {"type": "keyword"},
        "kind": {"type": "keyword"},
        "content": {"type": "text"},
        "confidence": {"type": "float"},
        "status": {"type": "keyword"},
        "storage_tier": {"type": "keyword"},
        "metadata": {"type": "object", "enabled": False},
    }}
}


class ResearchMemoryIndex:
    def __init__(self, *, elasticsearch_client: Any = None, milvus=None):
        self.elasticsearch = elasticsearch_client
        self.milvus = milvus

    @classmethod
    def from_environment(cls):
        elasticsearch = None
        url = os.getenv("ELASTICSEARCH_URL", "").strip()
        if url:
            from elasticsearch import Elasticsearch
            elasticsearch = Elasticsearch(url, request_timeout=10, retry_on_timeout=False)
        return cls(elasticsearch_client=elasticsearch)

    async def index(self, *, memory_id: str, user_id: str, kind: str,
                    content: str, confidence: float, metadata: dict[str, Any],
                    storage_tier: str = "hot") -> None:
        failures = []
        vector = await asyncio.to_thread(generate_embedding, content)
        if vector:
            try:
                milvus = self.milvus or get_milvus_service()
                await asyncio.to_thread(
                    milvus.insert_documents,
                    MEMORY_COLLECTION,
                    [{
                        "id": memory_id,
                        "doc_id": memory_id,
                        "kb_id": user_id,
                        "filename": kind,
                        "content": content,
                        "chunk_index": 0,
                        "vector": vector,
                    }],
                )
            except Exception as exc:
                failures.append(exc)
                logger.warning("memory vector indexing failed: %s", type(exc).__name__)
        if self.elasticsearch is not None:
            try:
                exists = await asyncio.to_thread(
                    self.elasticsearch.indices.exists, index=MEMORY_INDEX
                )
                if not exists:
                    await asyncio.to_thread(
                        self.elasticsearch.indices.create,
                        index=MEMORY_INDEX,
                        **MEMORY_INDEX_MAPPING,
                    )
                await asyncio.to_thread(
                    self.elasticsearch.index,
                    index=MEMORY_INDEX,
                    id=memory_id,
                    document={
                        "memory_id": memory_id,
                        "user_id": user_id,
                        "kind": kind,
                        "content": content,
                        "confidence": confidence,
                        "status": "active",
                        "storage_tier": storage_tier,
                        "metadata": metadata,
                    },
                )
            except Exception as exc:
                failures.append(exc)
                logger.warning("memory BM25 indexing failed: %s", type(exc).__name__)
        if failures and (not vector or self.elasticsearch is None or len(failures) > 1):
            raise RuntimeError("all configured memory indexes failed") from failures[0]

    async def update_tier(self, memory_id: str, storage_tier: str) -> None:
        """Keep the searchable cold catalog consistent with PostgreSQL."""

        if storage_tier not in {"hot", "cold"}:
            raise ValueError("storage_tier must be hot or cold")
        if self.elasticsearch is None:
            return
        try:
            await asyncio.to_thread(
                self.elasticsearch.update,
                index=MEMORY_INDEX,
                id=memory_id,
                doc={"storage_tier": storage_tier},
                refresh=False,
            )
        except Exception as exc:
            # PostgreSQL remains authoritative; retrieval always verifies the DB tier.
            logger.warning("memory tier index update failed: %s", type(exc).__name__)

    async def search(self, *, user_id: str, query: str, kinds: list[str], top_k: int):
        scores: dict[str, dict[str, float]] = defaultdict(
            lambda: {"vector": 0.0, "bm25": 0.0}
        )
        vector = await asyncio.to_thread(generate_embedding, query)
        if vector:
            try:
                milvus = self.milvus or get_milvus_service()
                rows = await asyncio.to_thread(
                    milvus.search, MEMORY_COLLECTION, vector, top_k * 2, user_id
                )
                for rank, row in enumerate(rows, start=1):
                    scores[str(row["id"])]["vector"] = 1.0 / rank
            except Exception as exc:
                logger.warning("memory vector retrieval failed: %s", type(exc).__name__)
        if self.elasticsearch is not None:
            try:
                response = await asyncio.to_thread(
                    self.elasticsearch.search,
                    index=MEMORY_INDEX,
                    query={"bool": {"must": [{"match": {"content": query}}], "filter": [
                        {"term": {"user_id": user_id}},
                        {"terms": {"kind": kinds}},
                        {"term": {"status": "active"}},
                    ]}},
                    size=top_k * 2,
                )
                for rank, row in enumerate(response.get("hits", {}).get("hits", []), start=1):
                    scores[str(row["_id"])]["bm25"] = 1.0 / rank
            except Exception as exc:
                logger.warning("memory BM25 retrieval failed: %s", type(exc).__name__)
        return scores
