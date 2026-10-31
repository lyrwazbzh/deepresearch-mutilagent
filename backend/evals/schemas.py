"""Stable contracts shared by report metrics, judges, and benchmark runners."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class Reference(BaseModel):
    id: str
    url: str | None = None
    title: str | None = None


class Claim(BaseModel):
    text: str = Field(min_length=1)
    supported: bool | None = None
    citation_ids: list[str] = Field(default_factory=list)


class EvalCase(BaseModel):
    """One reproducible report-evaluation sample.

    ``claims`` supports manually labelled factuality evaluation, while
    ``ground_truth`` and ``required_topics`` support ResearchBench-style cases.
    ``prediction``/``expected_answer`` are optional HotpotQA-compatible fields.
    """

    model_config = ConfigDict(extra="forbid")

    id: str
    query: str
    report: str
    references: list[Reference] = Field(default_factory=list)
    claims: list[Claim] = Field(default_factory=list)
    required_topics: list[str] = Field(default_factory=list)
    ground_truth: dict[str, Any] = Field(default_factory=dict)
    prediction: str | None = None
    expected_answer: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class RuleMetrics(BaseModel):
    """Fast, deterministic metrics in the 0-1 range.

    Hallucination rate is a risk metric (lower is better); all other fields are
    quality metrics (higher is better). Citation validity intentionally does
    not appear here: whether a source actually supports a claim is assessed by
    the citation-quality judge rather than by checking that an ID exists.
    """

    factual_accuracy: float = Field(ge=0.0, le=1.0)
    hallucination_rate: float = Field(ge=0.0, le=1.0)
    logical_consistency: float = Field(ge=0.0, le=1.0)
    citation_coverage: float = Field(ge=0.0, le=1.0)
    comprehensiveness: float = Field(ge=0.0, le=1.0)

    @property
    def bias(self) -> float:
        """Reference implementation's positive proxy for hallucination risk."""

        return 1.0 - self.hallucination_rate

    @property
    def factuality(self) -> float:
        """Compatibility alias for callers using the previous field name."""

        return self.factual_accuracy

    @property
    def topic_completeness(self) -> float:
        """Compatibility alias for callers using the previous field name."""

        return self.comprehensiveness

    def quality_dimensions(self) -> dict[str, float]:
        return {
            "factual_accuracy": self.factual_accuracy,
            "logical_consistency": self.logical_consistency,
            "citation_coverage": self.citation_coverage,
            "bias": self.bias,
            "comprehensiveness": self.comprehensiveness,
        }


class JudgeDimension(BaseModel):
    score: float = Field(ge=0.0, le=10.0)
    reason: str = ""


class JudgeScores(BaseModel):
    """Research report judge result aligned with deepresearch-agent-main."""

    factual_accuracy: JudgeDimension
    logical_consistency: JudgeDimension
    citation_quality: JudgeDimension
    comprehensiveness: JudgeDimension
    overall: JudgeDimension
    judge_backend: str = "callable"

    @property
    def dimension_mean(self) -> float:
        """Mean of the four independent quality dimensions (0-10)."""

        values = [
            self.factual_accuracy.score,
            self.logical_consistency.score,
            self.citation_quality.score,
            self.comprehensiveness.score,
        ]
        return sum(values) / len(values)

    @property
    def mean(self) -> float:
        values = [
            self.factual_accuracy.score,
            self.logical_consistency.score,
            self.citation_quality.score,
            self.comprehensiveness.score,
            self.overall.score,
        ]
        return sum(values) / len(values)

    def normalized_dimensions(self) -> dict[str, float]:
        return {
            "factual_accuracy": self.factual_accuracy.score / 10.0,
            "logical_consistency": self.logical_consistency.score / 10.0,
            "citation_quality": self.citation_quality.score / 10.0,
            "comprehensiveness": self.comprehensiveness.score / 10.0,
        }


class EvalResult(BaseModel):
    case_id: str
    rules: RuleMetrics
    judge: JudgeScores | None = None
    rule_score: float = Field(ge=0.0, le=1.0)
    judge_score: float | None = Field(default=None, ge=0.0, le=1.0)
    score: float = Field(ge=0.0, le=1.0)
    metadata: dict[str, Any] = Field(default_factory=dict)
