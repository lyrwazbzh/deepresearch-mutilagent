"""Deterministic hot/cold lifecycle policy for research memories."""

from __future__ import annotations

import math
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any


def _utc(value: datetime | None, fallback: datetime) -> datetime:
    if value is None:
        return fallback
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


@dataclass(frozen=True)
class MemoryTieringPolicy:
    """Score recency, access frequency and confidence on a common 0-1 scale."""

    recency_weight: float = 0.45
    frequency_weight: float = 0.25
    confidence_weight: float = 0.30
    recency_half_life_days: float = 90.0
    frequency_saturation: int = 8
    minimum_hot_days: float = 30.0
    cold_threshold: float = 0.35
    rehydrate_relevance_threshold: float = 0.55

    @classmethod
    def from_environment(cls) -> "MemoryTieringPolicy":
        return cls(
            recency_half_life_days=float(
                os.getenv("MEMORY_RECENCY_HALF_LIFE_DAYS", "90")
            ),
            frequency_saturation=int(
                os.getenv("MEMORY_FREQUENCY_SATURATION", "8")
            ),
            minimum_hot_days=float(os.getenv("MEMORY_MINIMUM_HOT_DAYS", "30")),
            cold_threshold=float(os.getenv("MEMORY_COLD_THRESHOLD", "0.35")),
            rehydrate_relevance_threshold=float(
                os.getenv("MEMORY_REHYDRATE_RELEVANCE_THRESHOLD", "0.55")
            ),
        )

    @staticmethod
    def confidence(row: Any) -> float:
        raw = getattr(row, "confidence", None)
        if raw is None:
            raw = getattr(row, "final_score", None)
            if raw is not None and float(raw) > 1.0:
                raw = float(raw) / 10.0
        if raw is None:
            raw = 0.5
        return min(max(float(raw), 0.0), 1.0)

    def heat_score(
        self,
        row: Any,
        *,
        now: datetime | None = None,
    ) -> float:
        now = now or datetime.now(timezone.utc)
        reference = _utc(
            getattr(row, "last_accessed_at", None)
            or getattr(row, "updated_at", None)
            or getattr(row, "created_at", None),
            now,
        )
        age_days = max((now - reference).total_seconds() / 86400.0, 0.0)
        half_life = max(self.recency_half_life_days, 0.01)
        recency = math.pow(0.5, age_days / half_life)
        accesses = max(int(getattr(row, "access_count", 0) or 0), 0)
        saturation = max(self.frequency_saturation, 1)
        frequency = min(
            math.log1p(accesses) / math.log1p(saturation),
            1.0,
        )
        score = (
            self.recency_weight * recency
            + self.frequency_weight * frequency
            + self.confidence_weight * self.confidence(row)
        )
        return round(min(max(score, 0.0), 1.0), 6)

    def should_move_to_cold(
        self,
        row: Any,
        *,
        now: datetime | None = None,
    ) -> bool:
        now = now or datetime.now(timezone.utc)
        if getattr(row, "status", "active") != "active":
            return False
        if getattr(row, "storage_tier", "hot") != "hot":
            return False
        created_at = _utc(getattr(row, "created_at", None), now)
        age_days = max((now - created_at).total_seconds() / 86400.0, 0.0)
        return (
            age_days >= self.minimum_hot_days
            and self.heat_score(row, now=now) < self.cold_threshold
        )

    def should_rehydrate(self, row: Any, *, retrieval_score: float) -> bool:
        return (
            getattr(row, "status", "active") == "active"
            and getattr(row, "storage_tier", "hot") == "cold"
            and float(retrieval_score) >= self.rehydrate_relevance_threshold
        )

