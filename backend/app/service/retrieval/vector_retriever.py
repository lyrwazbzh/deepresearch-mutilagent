"""Adapter over the existing Milvus retrieval service."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

from service.retrieval_service import retrieve_from_knowledge_base

from .types import RetrievalHit, RetrievalRequest


class VectorRetriever:
    def __init__(
        self,
        retrieve: Callable[..., list[dict[str, Any]]] = retrieve_from_knowledge_base,
    ):
        self._retrieve = retrieve

    async def search(self, request: RetrievalRequest) -> list[RetrievalHit]:
        rows = await asyncio.to_thread(
            self._retrieve,
            kb_name=request.kb_name,
            question=request.query,
            top_k=request.top_k,
        )
        hits: list[RetrievalHit] = []
        for rank, row in enumerate(rows, start=1):
            hits.append(
                RetrievalHit(
                    chunk_id=str(row.get("id", "")),
                    document_id=str(row.get("doc_id", "")),
                    content=str(row.get("content", "")),
                    filename=str(row.get("filename", "")),
                    source="vector",
                    score=float(row.get("score", 0.0) or 0.0),
                    rank=rank,
                    metadata={"chunk_index": row.get("chunk_index")},
                )
            )
        return hits
