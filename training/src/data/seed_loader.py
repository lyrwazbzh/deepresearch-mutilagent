"""External seed-question loader used by synthesis jobs."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class SeedQuestion(BaseModel):
    model_config = ConfigDict(extra="allow")

    id: str = Field(min_length=1, max_length=128)
    question: str = Field(min_length=3, max_length=10_000)
    industry: str = Field(min_length=1, max_length=128)
    question_type: str = Field(min_length=1, max_length=128)
    difficulty: Literal["easy", "medium", "hard"] = "medium"
    expected_tools: list[str] = Field(default_factory=list)
    parent_seed_id: str | None = None


def load_seed_questions(path: str | Path) -> list[SeedQuestion]:
    source = Path(path)
    if source.suffix == ".jsonl":
        rows = [
            json.loads(line)
            for line in source.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
    else:
        rows = json.loads(source.read_text(encoding="utf-8"))
    seeds = [SeedQuestion.model_validate(row) for row in rows]
    ids = [seed.id for seed in seeds]
    if len(ids) != len(set(ids)):
        raise ValueError("seed ids must be unique")
    return seeds
