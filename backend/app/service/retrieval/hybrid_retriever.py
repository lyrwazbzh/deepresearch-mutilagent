"""Fault-tolerant vector/BM25 hybrid retriever."""

from __future__ import annotations

import asyncio
import logging

from .fusion import reciprocal_rank_fusion
from .types import RetrievalHit, RetrievalRequest, Retriever


logger = logging.getLogger(__name__)


class HybridRetriever:
    def __init__(
        self,
        vector: Retriever,
        bm25: Retriever | None,
        *,
        vector_weight: float = 0.6,
        bm25_weight: float = 0.4,
        rrf_k: int = 60,
    ):
        self.vector = vector
        self.bm25 = bm25
        self.vector_weight = vector_weight
        self.bm25_weight = bm25_weight
        self.rrf_k = rrf_k

    async def search(self, request: RetrievalRequest) -> list[RetrievalHit]:
        if self.bm25 is None:
            return await self.vector.search(request)
        vector_result, bm25_result = await asyncio.gather(
            self.vector.search(request),
            self.bm25.search(request),
            return_exceptions=True,
        )
        ranked: list[tuple[float, list[RetrievalHit]]] = []
        if isinstance(vector_result, Exception):
            logger.warning("vector retrieval failed: %s", type(vector_result).__name__)
        else:
            ranked.append((self.vector_weight, vector_result))
        if isinstance(bm25_result, Exception):
            logger.warning("BM25 retrieval failed: %s", type(bm25_result).__name__)
        else:
            ranked.append((self.bm25_weight, bm25_result))
        if not ranked:
            raise RuntimeError("all retrieval backends failed")
        if len(ranked) == 1:
            return ranked[0][1][: request.top_k]
        return reciprocal_rank_fusion(
            ranked, rrf_k=self.rrf_k, top_k=request.top_k
        )
