"""Lazy factories keep Elasticsearch optional during unit tests."""

from __future__ import annotations

import os
from functools import lru_cache

from .bm25_retriever import BM25Retriever
from .hybrid_retriever import HybridRetriever
from .index_service import KnowledgeIndexService
from .vector_retriever import VectorRetriever


def _elasticsearch_client():
    url = os.getenv("ELASTICSEARCH_URL", "").strip()
    if not url:
        return None
    from elasticsearch import Elasticsearch

    return Elasticsearch(url, request_timeout=10, retry_on_timeout=False)


@lru_cache(maxsize=1)
def get_hybrid_retriever() -> HybridRetriever:
    client = _elasticsearch_client()
    return HybridRetriever(
        vector=VectorRetriever(),
        bm25=BM25Retriever(client) if client is not None else None,
    )


@lru_cache(maxsize=1)
def get_knowledge_index_service() -> KnowledgeIndexService | None:
    client = _elasticsearch_client()
    if client is None:
        return None
    from service.resilience.persistence import enqueue_search_index_failure

    return KnowledgeIndexService(client, enqueue_failure=enqueue_search_index_failure)
