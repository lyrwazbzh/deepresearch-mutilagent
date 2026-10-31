import asyncio

from service.retrieval.fusion import reciprocal_rank_fusion
from service.retrieval.hybrid_retriever import HybridRetriever
from service.retrieval.types import RetrievalHit, RetrievalRequest


def hit(chunk_id: str, source: str, score: float = 1.0) -> RetrievalHit:
    return RetrievalHit(
        chunk_id=chunk_id,
        document_id="doc",
        content=chunk_id,
        filename="file.txt",
        source=source,
        score=score,
    )


class FakeRetriever:
    def __init__(self, rows=None, error=None):
        self.rows = rows or []
        self.error = error

    async def search(self, request):
        if self.error:
            raise self.error
        return self.rows


def test_rrf_merges_duplicate_chunks_and_keeps_rank_order():
    rows = reciprocal_rank_fusion(
        [
            (0.6, [hit("shared", "vector"), hit("vector-only", "vector")]),
            (0.4, [hit("shared", "bm25"), hit("bm25-only", "bm25")]),
        ],
        top_k=3,
    )
    assert rows[0].chunk_id == "shared"
    assert rows[0].source == "hybrid"
    assert rows[0].metadata["retrievers"] == ["bm25", "vector"]


def test_hybrid_retrieval_falls_back_when_one_backend_fails():
    retriever = HybridRetriever(
        vector=FakeRetriever([hit("vector", "vector")]),
        bm25=FakeRetriever(error=TimeoutError()),
    )
    rows = asyncio.run(
        retriever.search(RetrievalRequest(query="q", kb_name="kb", top_k=5))
    )
    assert [row.chunk_id for row in rows] == ["vector"]
