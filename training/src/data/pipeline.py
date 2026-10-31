"""Validation, sanitization, deduplication and split pipeline."""

from __future__ import annotations

import json
import re
from collections import Counter
from pathlib import Path
from typing import Any, Iterable

from pydantic import ValidationError

from src.data.deduplicator import TrajectoryDeduplicator
from src.data.quality_scorer import score_trajectory
from src.data.schema_v3 import Trajectory, load_tool_schema, validate_tool_arguments
from src.data.splitter import assign_split


SENSITIVE_PATTERNS = (
    (re.compile(r"sk-[A-Za-z0-9]{16,}"), "[REDACTED_API_KEY]"),
    (re.compile(r"(?i)bearer\s+[A-Za-z0-9._-]{16,}"), "Bearer [REDACTED_TOKEN]"),
    (
        re.compile(r"(?i)(postgresql|postgres|mysql)://[^@\s]+@"),
        r"\1://[REDACTED_CREDENTIALS]@",
    ),
    (re.compile(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}"), "[REDACTED_EMAIL]"),
    (re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)"), "[REDACTED_PHONE]"),
    (re.compile(r"(?<!\d)\d{17}[0-9Xx](?!\d)"), "[REDACTED_ID]"),
)


def sanitize_text(value: str) -> str:
    for pattern, replacement in SENSITIVE_PATTERNS:
        value = pattern.sub(replacement, value)
    return value


def sanitize_value(value: Any) -> Any:
    if isinstance(value, str):
        return sanitize_text(value)
    if isinstance(value, list):
        return [sanitize_value(item) for item in value]
    if isinstance(value, dict):
        return {key: sanitize_value(item) for key, item in value.items()}
    return value


def question_text(trajectory: Trajectory) -> str:
    for message in trajectory.messages:
        if message.role == "user" and message.content:
            return message.content
    return ""


class DatasetBuilder:
    def __init__(
        self,
        *,
        tool_schema_path: str | Path,
        min_quality: float = 0.75,
    ):
        self.tool_schema = load_tool_schema(tool_schema_path)
        self.min_quality = min_quality
        self.deduplicator = TrajectoryDeduplicator()

    def build(
        self,
        rows: Iterable[dict[str, Any]],
        *,
        output_dir: str | Path,
        target_valid: int | None = None,
    ) -> dict[str, Any]:
        output = Path(output_dir)
        output.mkdir(parents=True, exist_ok=True)
        handles = {
            split: (output / f"{split}.jsonl").open("w", encoding="utf-8")
            for split in ("train", "dev", "test")
        }
        stats = Counter()
        try:
            for raw in rows:
                stats["raw_count"] += 1
                try:
                    trajectory = Trajectory.model_validate(sanitize_value(raw))
                except (ValidationError, ValueError, json.JSONDecodeError):
                    stats["schema_rejected"] += 1
                    continue
                argument_errors = validate_tool_arguments(
                    trajectory, self.tool_schema
                )
                if argument_errors:
                    stats["argument_rejected"] += 1
                    continue
                trajectory.metadata.quality_score = score_trajectory(trajectory)
                if trajectory.metadata.quality_score < self.min_quality:
                    stats["quality_rejected"] += 1
                    continue
                if self.deduplicator.is_duplicate(question_text(trajectory)):
                    stats["duplicate_rejected"] += 1
                    continue
                trajectory.refresh_content_hash()
                parent_seed_id = (trajectory.metadata.model_extra or {}).get("parent_seed_id")
                split = assign_split(parent_seed_id or trajectory.metadata.seed_id)
                handles[split].write(
                    trajectory.model_dump_json(exclude_none=True) + "\n"
                )
                stats[f"{split}_count"] += 1
                stats[f"difficulty_{trajectory.metadata.difficulty}"] += 1
                stats["valid_count"] += 1
                if target_valid and stats["valid_count"] >= target_valid:
                    break
        finally:
            for handle in handles.values():
                handle.close()

        manifest = {
            "dataset_version": "v1",
            "trajectory_schema_version": "3.0",
            "tool_schema_version": "1.0",
            "min_quality": self.min_quality,
            **dict(stats),
        }
        (output / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        if target_valid and stats["valid_count"] < target_valid:
            raise ValueError(
                f"only {stats['valid_count']} valid trajectories; target is {target_valid}"
            )
        return manifest


def read_jsonl(paths: Iterable[str | Path]) -> Iterable[dict[str, Any]]:
    for path in paths:
        with Path(path).open("r", encoding="utf-8") as handle:
            for line in handle:
                if line.strip():
                    yield json.loads(line)
