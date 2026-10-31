"""Fast and reproducible report metrics adapted from deepresearch-agent-main."""

from __future__ import annotations

import math
import re
from collections.abc import Callable, Sequence
from typing import Any

from .schemas import EvalCase, RuleMetrics


_CITATION = re.compile(r"\[(?:ref[:_-]?)?([A-Za-z0-9._-]+)\]", re.IGNORECASE)
_SENTENCE_SPLIT = re.compile(r"[。！？!?\n]")


def _ratio(numerator: int, denominator: int, *, empty: float = 0.0) -> float:
    return numerator / denominator if denominator else empty


class RuleBasedMetrics:
    """Rule/statistical metrics used for cheap batch evaluation and CI gates."""

    @staticmethod
    def fact_accuracy(report: str, ground_truth: dict[str, Any] | None = None) -> float:
        """Measure the proportion of expected facts mentioned in the report."""

        if not ground_truth:
            return 0.0
        normalized = report.casefold()
        matched = sum(str(fact).casefold() in normalized for fact in ground_truth)
        return matched / len(ground_truth)

    @staticmethod
    def semantic_fact_accuracy(
        report: str,
        ground_truth: dict[str, Any] | None,
        embed: Callable[[list[str]], Sequence[Sequence[float]]],
        *,
        threshold: float = 0.65,
    ) -> float:
        """Measure semantic fact coverage using an injected embedding function.

        The embedding dependency is injected so offline evaluation never starts
        a network client merely by importing the metrics package.
        """

        if not ground_truth:
            return 0.0
        chunks = [part.strip() for part in _SENTENCE_SPLIT.split(report) if len(part.strip()) > 10]
        if not chunks:
            return 0.0
        facts = [f"{key}：{value}" for key, value in ground_truth.items()]
        vectors = [list(vector) for vector in embed(chunks + facts)]
        if len(vectors) != len(chunks) + len(facts):
            raise ValueError("embed must return one vector for every input text")

        chunk_vectors = vectors[: len(chunks)]
        fact_vectors = vectors[len(chunks) :]

        def cosine(left: Sequence[float], right: Sequence[float]) -> float:
            if len(left) != len(right):
                raise ValueError("embedding vectors must use one dimension")
            numerator = sum(a * b for a, b in zip(left, right, strict=True))
            left_norm = math.sqrt(sum(value * value for value in left))
            right_norm = math.sqrt(sum(value * value for value in right))
            if left_norm == 0.0 or right_norm == 0.0:
                return 0.0
            return numerator / (left_norm * right_norm)

        covered = sum(
            max(cosine(fact, chunk) for chunk in chunk_vectors) >= threshold
            for fact in fact_vectors
        )
        return covered / len(fact_vectors)

    @staticmethod
    def hallucination_rate(report: str) -> float:
        """Estimate unsupported-claim risk with deterministic text heuristics."""

        sentences = [part.strip() for part in _SENTENCE_SPLIT.split(report) if part.strip()]
        if not sentences:
            return 1.0

        indicators = (
            re.compile(r"\d+[\d,]*\.?\d*\s*(?:%|倍|个|人|元|美元|亿|万)"),
            re.compile(r"毫无疑问|绝对|必然|一定|众所周知"),
            re.compile(r"据我所知|据了解|研究显示[^【\[（(]"),
        )
        suspicious = 0
        for sentence in sentences:
            if _CITATION.search(sentence) or re.search(r"https?://", sentence):
                continue
            if any(pattern.search(sentence) for pattern in indicators):
                suspicious += 1
        return min(1.0, suspicious / len(sentences))

    @staticmethod
    def citation_coverage(report: str) -> float:
        """Compute the proportion of non-empty paragraphs carrying a citation."""

        paragraphs = [paragraph.strip() for paragraph in report.splitlines() if paragraph.strip()]
        if not paragraphs:
            return 0.0
        patterns = (
            _CITATION,
            re.compile(r"\[来源[：:]|【来源[：:]|\(来源[：:]"),
            re.compile(r"https?://|arxiv\.org", re.IGNORECASE),
        )
        cited = sum(any(pattern.search(paragraph) for pattern in patterns) for paragraph in paragraphs)
        return cited / len(paragraphs)

    @staticmethod
    def logical_consistency(report: str) -> float:
        """Estimate local contradiction risk and reward explicit reasoning links."""

        sentences = [part.strip() for part in _SENTENCE_SPLIT.split(report) if part.strip()]
        if not sentences:
            return 0.0
        contradiction_pairs = (
            ("是", "不是"),
            ("可以", "不可以"),
            ("会", "不会"),
            ("支持", "反对"),
            ("增加", "减少"),
        )
        contradictions = sum(
            any(left in sentence and right in sentence for left, right in contradiction_pairs)
            for sentence in sentences
        )
        connectives = ("因此", "所以", "然而", "但是", "首先", "其次", "综上所述")
        bonus = min(0.1, sum(connective in report for connective in connectives) * 0.01)
        return min(1.0, max(0.0, 1.0 - contradictions / len(sentences) + bonus))

    @staticmethod
    def comprehensiveness(report: str, expected_topics: list[str] | None = None) -> float:
        """Measure explicit coverage of expected subtopics."""

        if not expected_topics:
            return 0.0
        normalized = report.casefold()
        covered = sum(topic.casefold() in normalized for topic in expected_topics)
        return covered / len(expected_topics)

    @staticmethod
    def composite_score(
        metrics: dict[str, float],
        weights: dict[str, float] | None = None,
    ) -> float:
        """Aggregate rule metrics with the reference implementation's weights."""

        selected = weights or {
            "factual_accuracy": 0.25,
            "logical_consistency": 0.20,
            "citation_coverage": 0.20,
            "bias": 0.20,
            "comprehensiveness": 0.15,
        }
        total_weight = sum(selected.values())
        if total_weight <= 0:
            return 0.0
        return sum(metrics.get(name, 0.0) * weight for name, weight in selected.items()) / total_weight

    @staticmethod
    def efficiency_score(
        num_turns: int,
        *,
        target_turns: float = 8.0,
        slope: float = 0.5,
        max_bonus: float = 0.5,
    ) -> float:
        """Sigmoid efficiency score retained from the reference evaluator."""

        return max_bonus + (1.0 - max_bonus) / (
            1.0 + math.exp(slope * (num_turns - target_turns))
        )


def evaluate_rules(case: EvalCase) -> RuleMetrics:
    """Evaluate one case, preferring labelled claims over text heuristics."""

    labeled_claims = [claim for claim in case.claims if claim.supported is not None]
    if labeled_claims:
        supported = sum(claim.supported is True for claim in labeled_claims)
        factual_accuracy = _ratio(supported, len(labeled_claims))
        hallucination_rate = _ratio(len(labeled_claims) - supported, len(labeled_claims))
    else:
        factual_accuracy = RuleBasedMetrics.fact_accuracy(case.report, case.ground_truth)
        hallucination_rate = RuleBasedMetrics.hallucination_rate(case.report)

    if case.claims:
        cited_in_report = set(_CITATION.findall(case.report))
        cited_claims = sum(
            bool(set(claim.citation_ids) & cited_in_report) for claim in case.claims
        )
        citation_coverage = _ratio(cited_claims, len(case.claims))
    else:
        citation_coverage = RuleBasedMetrics.citation_coverage(case.report)

    return RuleMetrics(
        factual_accuracy=factual_accuracy,
        hallucination_rate=hallucination_rate,
        logical_consistency=RuleBasedMetrics.logical_consistency(case.report),
        citation_coverage=citation_coverage,
        comprehensiveness=RuleBasedMetrics.comprehensiveness(
            case.report,
            case.required_topics,
        ),
    )
