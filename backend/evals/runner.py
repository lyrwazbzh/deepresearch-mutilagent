"""JSONL evaluation runner and release gate."""

from __future__ import annotations

import json
from pathlib import Path

from .composite import compute_composite_score
from .rule_metrics import evaluate_rules
from .schemas import EvalCase, EvalResult
from .statistics import bootstrap_ci_paired, cohens_d, paired_deltas


class EvaluationRunner:
    def __init__(self, judge=None):
        self.judge = judge

    async def evaluate(self, cases: list[EvalCase]) -> list[EvalResult]:
        results = []
        for case in cases:
            rules = evaluate_rules(case)
            judge = await self.judge.evaluate(case) if self.judge is not None else None
            composite = compute_composite_score(rules, judge)
            results.append(EvalResult(
                case_id=case.id,
                rules=rules,
                judge=judge,
                rule_score=composite["rule_score"],
                judge_score=composite["judge_score"],
                score=composite["composite_score"],
                metadata={
                    "aggregation": {"rule": 0.6, "judge": 0.4},
                },
            ))
        return results

    @staticmethod
    def load_jsonl(path: str | Path) -> list[EvalCase]:
        lines = Path(path).read_text(encoding="utf-8").splitlines()
        return [EvalCase.model_validate_json(line) for line in lines if line.strip()]

    @staticmethod
    def write_jsonl(path: str | Path, results: list[EvalResult]) -> None:
        output = "\n".join(result.model_dump_json() for result in results) + "\n"
        Path(path).write_text(output, encoding="utf-8")

    @staticmethod
    def summary(results: list[EvalResult]) -> dict:
        if not results:
            return {"cases": 0}
        score_stats = bootstrap_ci_paired([item.score for item in results])
        summary = {
            "cases": len(results),
            "score": sum(item.score for item in results) / len(results),
            "bootstrap_95_ci": [score_stats["ci_lower"], score_stats["ci_upper"]],
            "factual_accuracy": sum(
                item.rules.factual_accuracy for item in results
            ) / len(results),
            "hallucination_rate": sum(
                item.rules.hallucination_rate for item in results
            ) / len(results),
            "logical_consistency": sum(
                item.rules.logical_consistency for item in results
            ) / len(results),
            "citation_coverage": sum(
                item.rules.citation_coverage for item in results
            ) / len(results),
            "comprehensiveness": sum(
                item.rules.comprehensiveness for item in results
            ) / len(results),
        }
        judged = [item for item in results if item.judge_score is not None]
        if judged:
            summary["judge_score"] = sum(
                item.judge_score for item in judged if item.judge_score is not None
            ) / len(judged)
        return summary

    @staticmethod
    def compare_and_gate(
        baseline: list[EvalResult],
        candidate: list[EvalResult],
        *,
        max_regression: float = 0.01,
    ) -> dict:
        deltas = paired_deltas(
            {item.case_id: item.score for item in baseline},
            {item.case_id: item.score for item in candidate},
        )
        statistics = bootstrap_ci_paired(deltas)
        baseline_scores = [item.score for item in baseline if item.case_id in {
            candidate_item.case_id for candidate_item in candidate
        }]
        candidate_by_id = {item.case_id: item.score for item in candidate}
        candidate_scores = [
            candidate_by_id[item.case_id]
            for item in baseline
            if item.case_id in candidate_by_id
        ]
        return {
            "passed": statistics["ci_lower"] >= -max_regression,
            "mean_delta": statistics["mean_diff"],
            "bootstrap_95_ci": [statistics["ci_lower"], statistics["ci_upper"]],
            "p_value": statistics["p_value"],
            "cohens_d": round(cohens_d(candidate_scores, baseline_scores), 4),
            "max_regression": max_regression,
        }
