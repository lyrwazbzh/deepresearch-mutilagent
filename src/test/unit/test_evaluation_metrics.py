from __future__ import annotations

import random

from src.agent.evaluation.research_evaluation import (
    ImmutableDataset,
    ResourceManifest,
    SkillCaseOutput,
    SkillEvalSuite,
    precision_recall_f1,
    recall_at_k,
)
from src.test.harness import DeterministicIdFactory, FixedRandomSeed, TestClock


def test_recall_at_five_is_true_recall_and_classification_metrics() -> None:
    assert recall_at_k(["a", "x", "b", "z", "y"], ["a", "b", "c"], 5) == 2 / 3
    assert precision_recall_f1(2, 1, 1) == (2 / 3, 2 / 3, 2 / 3)


def test_immutable_dataset_and_deterministic_harness() -> None:
    dataset = ImmutableDataset(
        suite="rag",
        version="v1",
        cases=({"case_id": "rag_1", "query": "q"},),
    )
    assert dataset.dataset_ref == ImmutableDataset(
        suite="rag",
        version="v1",
        cases=({"case_id": "rag_1", "query": "q"},),
    ).dataset_ref
    ids = DeterministicIdFactory("phase12")
    assert [ids("eval"), ids("eval")] == ["eval_phase12_0001", "eval_phase12_0002"]
    clock = TestClock()
    before = clock.now()
    assert clock.advance(seconds=5) > before
    random.seed(999)
    with FixedRandomSeed(42):
        first = random.random()
    with FixedRandomSeed(42):
        assert random.random() == first


def test_skill_trigger_and_paired_effect_metrics(make_container) -> None:
    container, _ = make_container({})
    calls: list[tuple[bool, int]] = []

    def execute(case, enabled, seed, eval_scope, manifest):
        del eval_scope
        calls.append((enabled, seed))
        manifest.add("tasks", f"{case['case_id']}:{enabled}")
        return SkillCaseOutput(
            activated_skills=(
                frozenset(case["expected_skills"]) if enabled else frozenset()
            ),
            assertion_results={"chart_exists": enabled},
            success=enabled,
            diagnostics={"provider_failed": False},
        )

    suite = SkillEvalSuite(
        container.skill_registry,
        container.skill_runtime,
        execute,
        random_seed=73,
    )
    result = suite.execute_case(
        {
            "case_id": "skill_1",
            "expected_skills": ["data-analysis"],
            "result_assertions": ["chart_exists"],
        },
        "scope_1",
        ResourceManifest(),
    )
    assert result.metrics["trigger_tp"] == 1
    assert result.metrics["effect_success"] is True
    assert suite.summarize([result]) == {
        "trigger_precision": 1.0,
        "trigger_recall": 1.0,
        "trigger_f1": 1.0,
        "effect_success_rate": 1.0,
    }
    assert calls == [(True, 73), (False, 73)]
