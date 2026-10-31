"""Rule/Judge score aggregation adapted from the reference evaluation stack."""

from __future__ import annotations

from typing import Any

from .rule_metrics import RuleBasedMetrics
from .schemas import JudgeScores, RuleMetrics


def compute_composite_score(
    rule_metrics: RuleMetrics,
    judge_result: JudgeScores | None = None,
    weights: dict[str, float] | None = None,
) -> dict[str, Any]:
    """Combine deterministic metrics and Judge dimensions.

    The default split follows deepresearch-agent-main: 60% rules and 40%
    Judge. If no Judge is configured, the available rule score is normalized
    to full weight instead of being artificially capped at 0.6.
    """

    outer_weights = weights or {"rule": 0.6, "judge": 0.4}
    rule_score = RuleBasedMetrics.composite_score(rule_metrics.quality_dimensions())
    judge_dimensions = (
        judge_result.normalized_dimensions() if judge_result is not None else {}
    )
    judge_score = (
        sum(judge_dimensions.values()) / len(judge_dimensions)
        if judge_dimensions
        else None
    )

    available = {"rule": rule_score}
    if judge_score is not None:
        available["judge"] = judge_score
    denominator = sum(outer_weights.get(name, 0.0) for name in available)
    composite = (
        sum(outer_weights.get(name, 0.0) * value for name, value in available.items())
        / denominator
        if denominator > 0
        else 0.0
    )

    return {
        "composite_score": round(composite, 4),
        "rule_score": round(rule_score, 4),
        "judge_score": round(judge_score, 4) if judge_score is not None else None,
        "rule_metrics": rule_metrics.quality_dimensions(),
        "judge_dimensions": judge_dimensions,
    }
