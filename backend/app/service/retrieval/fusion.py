"""Rank fusion for scores that are not directly comparable."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable

from .types import RetrievalHit


def reciprocal_rank_fusion(
    ranked_lists: Iterable[tuple[float, list[RetrievalHit]]],
    *,
    rrf_k: int = 60,
    top_k: int = 10,
) -> list[RetrievalHit]:
    scores: dict[str, float] = defaultdict(float)
    selected: dict[str, RetrievalHit] = {}
    sources: dict[str, set[str]] = defaultdict(set)
    for weight, hits in ranked_lists:
        for rank, hit in enumerate(hits, start=1):
            key = hit.chunk_id or f"{hit.document_id}:{hit.content[:80]}"
            scores[key] += weight / (rrf_k + rank)
            sources[key].add(hit.source)
            selected.setdefault(key, hit)

    ordered = sorted(scores, key=scores.__getitem__, reverse=True)[:top_k]
    result: list[RetrievalHit] = []
    for rank, key in enumerate(ordered, start=1):
        hit = selected[key]
        hit.score = scores[key]
        hit.rank = rank
        hit.source = "hybrid" if len(sources[key]) > 1 else next(iter(sources[key]))
        hit.metadata = {**hit.metadata, "retrievers": sorted(sources[key])}
        result.append(hit)
    return result
