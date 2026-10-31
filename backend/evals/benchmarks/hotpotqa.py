"""HotpotQA multi-hop answer metrics adapted from deepresearch-agent-main."""

from __future__ import annotations

import json
import random
import re
from collections import Counter
from pathlib import Path
from typing import Any


class HotpotQABenchmark:
    """Load HotpotQA-compatible records and compute EM/F1/pass@k metrics."""

    MOCK_DATA: list[dict[str, Any]] = [
        {
            "question": "《红楼梦》的作者曹雪芹生活在哪个朝代？",
            "answer": "清朝",
            "type": "bridge",
            "level": "easy",
            "context": [
                ["红楼梦", ["《红楼梦》是中国古典小说。"]],
                ["曹雪芹", ["曹雪芹是清代小说家。"]],
            ],
        }
    ]

    def __init__(
        self,
        data_path: str | Path | None = None,
        *,
        data: list[dict[str, Any]] | None = None,
        use_mock: bool = False,
    ) -> None:
        if data is not None:
            self.data = data
        elif data_path is not None:
            payload = json.loads(Path(data_path).read_text(encoding="utf-8"))
            self.data = payload if isinstance(payload, list) else payload.get("data", [])
        elif use_mock:
            self.data = list(self.MOCK_DATA)
        else:
            self.data = []

    @staticmethod
    def to_research_format(sample: dict[str, Any]) -> dict[str, Any]:
        contexts = sample.get("context", [])
        sections = []
        for context in contexts:
            if isinstance(context, (list, tuple)) and len(context) == 2:
                title, sentences = context
                sections.append(f"## {title}\n{' '.join(sentences)}")
        return {
            "query": sample.get("question", ""),
            "context": "\n".join(sections),
            "expected_answer": sample.get("answer", ""),
            "type": sample.get("type", "bridge"),
            "level": sample.get("level", "medium"),
        }

    def get_samples(
        self,
        n: int | None = None,
        *,
        shuffle: bool = False,
        seed: int = 42,
    ) -> list[dict[str, Any]]:
        samples = [self.to_research_format(item) for item in self.data]
        if shuffle:
            random.Random(seed).shuffle(samples)
        return samples[:n] if n is not None else samples

    @staticmethod
    def normalize_answer(text: str) -> str:
        normalized = text.casefold().strip()
        normalized = re.sub(r"\b(a|an|the)\b", " ", normalized)
        normalized = re.sub(r"[^\w\s]", "", normalized)
        return " ".join(normalized.split())

    @classmethod
    def exact_match(cls, prediction: str, gold: str) -> bool:
        return cls.normalize_answer(prediction) == cls.normalize_answer(gold)

    @classmethod
    def f1_score(cls, prediction: str, gold: str) -> float:
        prediction_tokens = cls.normalize_answer(prediction).split()
        gold_tokens = cls.normalize_answer(gold).split()
        if not prediction_tokens and not gold_tokens:
            return 1.0
        if not prediction_tokens or not gold_tokens:
            return 0.0
        overlap = sum((Counter(prediction_tokens) & Counter(gold_tokens)).values())
        if overlap == 0:
            return 0.0
        precision = overlap / len(prediction_tokens)
        recall = overlap / len(gold_tokens)
        return 2.0 * precision * recall / (precision + recall)

    @classmethod
    def pass_at_k(cls, predictions: list[str], gold: str, k: int = 1) -> bool:
        return any(cls.exact_match(item, gold) for item in predictions[:k])

    @classmethod
    def gold_entity_coverage(cls, report: str, gold: str) -> float:
        stopwords = {
            "a", "an", "the", "and", "or", "in", "on", "at", "to", "of",
            "for", "with", "is", "was", "are", "were", "be", "been", "by",
        }
        tokens = [
            token
            for token in cls.normalize_answer(gold).split()
            if token not in stopwords and len(token) > 1
        ]
        if not tokens or not report:
            return 0.0
        normalized_report = report.casefold()
        return sum(token in normalized_report for token in tokens) / len(tokens)

    def evaluate(
        self,
        predictions: list[dict[str, Any]],
        metrics: list[str] | None = None,
    ) -> dict[str, float]:
        selected = metrics or ["em", "f1", "pass@1"]
        if not predictions:
            return {metric: 0.0 for metric in selected}
        output: dict[str, float] = {}
        if "em" in selected:
            output["exact_match"] = sum(
                self.exact_match(item.get("prediction", ""), item.get("gold", ""))
                for item in predictions
            ) / len(predictions)
        if "f1" in selected:
            output["f1"] = sum(
                self.f1_score(item.get("prediction", ""), item.get("gold", ""))
                for item in predictions
            ) / len(predictions)
        if "pass@1" in selected:
            output["pass@1"] = sum(
                self.pass_at_k([item.get("prediction", "")], item.get("gold", ""))
                for item in predictions
            ) / len(predictions)
        reports = [item for item in predictions if item.get("report")]
        if reports:
            output["gold_entity_coverage"] = sum(
                self.gold_entity_coverage(item["report"], item.get("gold", ""))
                for item in reports
            ) / len(reports)
        return output
