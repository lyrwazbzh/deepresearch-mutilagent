"""Serializable evaluation report container."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


class EvaluationReport:
    def __init__(self, name: str, num_questions: int = 0) -> None:
        self.name = name
        self.num_questions = num_questions
        self.timestamp = datetime.now(UTC).isoformat()
        self.details: list[dict[str, Any]] = []
        self.summary: dict[str, Any] = {}

    def add_detail(self, detail: dict[str, Any]) -> None:
        self.details.append(detail)

    def set_summary(self, summary: dict[str, Any]) -> None:
        self.summary = summary

    def to_dict(self) -> dict[str, Any]:
        return {
            "evaluation_name": self.name,
            "timestamp": self.timestamp,
            "num_questions": self.num_questions,
            "summary": self.summary,
            "details": self.details,
        }

    def save(self, output_dir: str | Path, filename: str | None = None) -> Path:
        directory = Path(output_dir)
        directory.mkdir(parents=True, exist_ok=True)
        target = directory / (
            filename
            or f"{self.name}_{datetime.now(UTC).strftime('%Y%m%d_%H%M%S')}.json"
        )
        target.write_text(
            json.dumps(self.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return target

    def to_markdown(self) -> str:
        lines = [
            f"# {self.name}",
            "",
            f"- **评测时间**: {self.timestamp}",
            f"- **题目数量**: {self.num_questions}",
            "",
            "## 汇总",
            "",
        ]
        lines.extend(f"- **{key}**: {value}" for key, value in self.summary.items())
        lines.extend(["", "## 明细", ""])
        for detail in self.details:
            lines.append(f"### {detail.get('question_id', detail.get('case_id', 'unknown'))}")
            lines.extend(
                f"- {key}: {value}"
                for key, value in detail.items()
                if key not in {"question_id", "case_id"}
            )
            lines.append("")
        return "\n".join(lines)
