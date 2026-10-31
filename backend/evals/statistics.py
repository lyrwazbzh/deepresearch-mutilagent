"""Bootstrap confidence intervals, effect sizes, and paired comparisons."""

from __future__ import annotations

import math
import random
import statistics
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ConfidenceInterval:
    mean: float
    low: float
    high: float


def _percentile(values: list[float], probability: float) -> float:
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = probability * (len(ordered) - 1)
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    fraction = position - lower
    return ordered[lower] * (1.0 - fraction) + ordered[upper] * fraction


def bootstrap_mean_ci(
    values: list[float],
    *,
    confidence: float = 0.95,
    samples: int = 2000,
    seed: int = 42,
) -> ConfidenceInterval:
    if not values:
        raise ValueError("values must not be empty")
    if samples <= 0:
        raise ValueError("samples must be positive")
    rng = random.Random(seed)
    sample_size = len(values)
    means = [
        sum(values[rng.randrange(sample_size)] for _ in range(sample_size))
        / sample_size
        for _ in range(samples)
    ]
    alpha = (1.0 - confidence) / 2.0
    return ConfidenceInterval(
        mean=statistics.fmean(values),
        low=_percentile(means, alpha),
        high=_percentile(means, 1.0 - alpha),
    )


def paired_deltas(baseline: dict[str, float], candidate: dict[str, float]) -> list[float]:
    common = sorted(set(baseline) & set(candidate))
    if not common:
        raise ValueError("baseline and candidate have no common case ids")
    return [candidate[key] - baseline[key] for key in common]


def bootstrap_ci_paired(
    diffs: list[float],
    *,
    n_bootstrap: int = 10000,
    confidence: float = 0.95,
    seed: int = 42,
) -> dict[str, Any]:
    """Bootstrap the paired candidate-minus-baseline difference."""

    if not diffs:
        return {
            "mean_diff": 0.0,
            "ci_lower": 0.0,
            "ci_upper": 0.0,
            "p_value": 1.0,
            "significant": False,
            "n": 0,
        }
    interval = bootstrap_mean_ci(
        diffs,
        confidence=confidence,
        samples=n_bootstrap,
        seed=seed,
    )
    rng = random.Random(seed)
    n = len(diffs)
    bootstrap_means = [
        sum(diffs[rng.randrange(n)] for _ in range(n)) / n
        for _ in range(n_bootstrap)
    ]
    p_value = sum(value <= 0.0 for value in bootstrap_means) / n_bootstrap
    return {
        "mean_diff": round(interval.mean, 4),
        "ci_lower": round(interval.low, 4),
        "ci_upper": round(interval.high, 4),
        "p_value": round(p_value, 4),
        "significant": interval.low > 0.0,
        "n": n,
    }


def bootstrap_ci_two_sample(
    scores_a: list[float],
    scores_b: list[float],
    *,
    n_bootstrap: int = 10000,
    confidence: float = 0.95,
    seed: int = 42,
) -> dict[str, Any]:
    """Bootstrap the difference between two independent sample means."""

    if not scores_a or not scores_b:
        return {
            "mean_diff": 0.0,
            "ci_lower": 0.0,
            "ci_upper": 0.0,
            "p_value": 1.0,
            "significant": False,
            "n_a": len(scores_a),
            "n_b": len(scores_b),
        }
    rng = random.Random(seed)
    differences = []
    for _ in range(n_bootstrap):
        mean_a = statistics.fmean(rng.choice(scores_a) for _ in scores_a)
        mean_b = statistics.fmean(rng.choice(scores_b) for _ in scores_b)
        differences.append(mean_a - mean_b)
    alpha = (1.0 - confidence) / 2.0
    lower = _percentile(differences, alpha)
    upper = _percentile(differences, 1.0 - alpha)
    return {
        "mean_diff": round(statistics.fmean(scores_a) - statistics.fmean(scores_b), 4),
        "ci_lower": round(lower, 4),
        "ci_upper": round(upper, 4),
        "p_value": round(sum(value <= 0.0 for value in differences) / n_bootstrap, 4),
        "significant": lower > 0.0,
        "n_a": len(scores_a),
        "n_b": len(scores_b),
    }


def cohens_d(scores_a: list[float], scores_b: list[float]) -> float:
    """Calculate Cohen's d using the reference evaluator's pooled variance."""

    if len(scores_a) < 2 or len(scores_b) < 2:
        return 0.0
    pooled_variance = (statistics.variance(scores_a) + statistics.variance(scores_b)) / 2.0
    if pooled_variance <= 1e-18:
        return 0.0
    return (statistics.fmean(scores_a) - statistics.fmean(scores_b)) / math.sqrt(
        pooled_variance
    )


def paired_t_test(scores_a: list[float], scores_b: list[float]) -> dict[str, Any]:
    """Run a paired t-test when SciPy exists; otherwise return bootstrap stats."""

    if len(scores_a) != len(scores_b):
        raise ValueError("paired samples must have equal length")
    try:
        from scipy import stats
    except ImportError:
        return bootstrap_ci_paired(
            [left - right for left, right in zip(scores_a, scores_b, strict=True)]
        )
    result = stats.ttest_rel(scores_a, scores_b)
    return {
        "t_statistic": round(float(result.statistic), 4),
        "p_value": round(float(result.pvalue), 4),
        "mean_diff": round(
            statistics.fmean(
                left - right
                for left, right in zip(scores_a, scores_b, strict=True)
            ),
            4,
        ),
        "n": len(scores_a),
    }
