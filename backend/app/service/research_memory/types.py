"""Public research-memory schemas."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class FactMemoryInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    subject: str = Field(min_length=1, max_length=500)
    predicate: str = Field(min_length=1, max_length=255)
    value: str = Field(min_length=1)
    content: str = Field(min_length=1)
    source_url: str | None = None
    source_name: str | None = None
    source_type: str | None = None
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    observed_at: datetime | None = None
    run_id: str
    metadata: dict[str, Any] = Field(default_factory=dict)


class ExperienceMemoryInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scenario: str = Field(min_length=1)
    trigger: str = Field(min_length=1)
    strategy: str = Field(min_length=1)
    outcome: str = Field(min_length=1)
    critic_summary: str | None = None
    initial_score: float | None = None
    final_score: float | None = None
    run_id: str
    metadata: dict[str, Any] = Field(default_factory=dict)


class MemoryQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")

    user_id: str
    query: str = Field(min_length=1)
    kinds: list[Literal["fact", "experience"]] = Field(
        default_factory=lambda: ["fact", "experience"]
    )
    top_k: int = Field(default=5, ge=1, le=20)
    min_confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    include_archived: bool = False
    allow_cold_rehydrate: bool = True


class MemoryHit(BaseModel):
    id: str
    kind: Literal["fact", "experience"]
    content: str
    score: float
    vector_score: float = 0.0
    bm25_score: float = 0.0
    time_score: float = 0.0
    confidence: float = 0.0
    storage_tier: Literal["hot", "cold"] = "hot"
    rehydrated: bool = False
    metadata: dict[str, Any] = Field(default_factory=dict)
