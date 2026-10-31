"""Elasticsearch BM25 adapter for knowledge chunks."""

from __future__ import annotations

import asyncio
from typing import Any

from .types import RetrievalHit, RetrievalRequest


KNOWLEDGE_CHUNK_INDEX = "knowledge_chunks_v1"


def normalized_kb_name(value: str) -> str:
    normalized = value.lower().replace(" ", "_")
    return normalized if normalized.startswith("kb_") else f"kb_{normalized}"


class BM25Retriever:
    def __init__(self, client: Any, *, index_name: str = KNOWLEDGE_CHUNK_INDEX):
        self.client = client
        self.index_name = index_name

    async def search(self, request: RetrievalRequest) -> list[RetrievalHit]:
        filters: list[dict[str, Any]] = [
            {"term": {"kb_name": normalized_kb_name(request.kb_name)}}
        ]
        if request.user_id:
            filters.append({"term": {"user_id": request.user_id}})
        query = {
            "bool": {
                "must": [
                    {
                        "multi_match": {
                            "query": request.query,
                            "fields": ["content^2", "filename"],
                            "type": "best_fields",
                        }
                    }
                ],
                "filter": filters,
            }
        }
        response = await asyncio.to_thread(
            self.client.search,
            index=self.index_name,
            query=query,
            size=request.top_k,
        )
        hits: list[RetrievalHit] = []
        for rank, item in enumerate(response.get("hits", {}).get("hits", []), start=1):
            source = item.get("_source", {})
            hits.append(
                RetrievalHit(
                    chunk_id=str(source.get("chunk_id", item.get("_id", ""))),
                    document_id=str(source.get("document_id", "")),
                    content=str(source.get("content", "")),
                    filename=str(source.get("filename", "")),
                    source="bm25",
                    score=float(item.get("_score", 0.0) or 0.0),
                    rank=rank,
                    metadata=source.get("metadata") or {},
                )
            )
        return hits
