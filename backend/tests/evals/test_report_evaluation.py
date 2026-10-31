import pytest

from evals.benchmarks.hotpotqa import HotpotQABenchmark
from evals.benchmarks.research_bench import ResearchBench
from evals.composite import compute_composite_score
from evals.llm_judge import LLMJudge
from evals.rule_metrics import RuleBasedMetrics, evaluate_rules
from evals.runner import EvaluationRunner
from evals.schemas import Claim, EvalCase, Reference
from evals.statistics import bootstrap_ci_paired, cohens_d


def sample_case(case_id="case-1"):
    return EvalCase(
        id=case_id,
        query="储能市场趋势",
        report="储能装机增长。[1]\n政策推动需求。[1]",
        references=[Reference(id="1", url="https://example.test/1")],
        claims=[Claim(text="装机增长", supported=True, citation_ids=["1"])],
        required_topics=["储能", "政策"],
    )


def test_reference_rule_metric_dimensions():
    metrics = evaluate_rules(sample_case())
    assert metrics.factual_accuracy == 1.0
    assert metrics.hallucination_rate == 0.0
    assert metrics.logical_consistency == 1.0
    assert metrics.citation_coverage == 1.0
    assert metrics.comprehensiveness == 1.0
    assert RuleBasedMetrics.composite_score(metrics.quality_dimensions()) == 1.0


@pytest.mark.asyncio
async def test_five_item_judge_and_sixty_forty_composite():
    judge = LLMJudge(lambda _: """{
        "factual_accuracy": {"score": 8, "reason": "ok"},
        "logical_consistency": {"score": 9, "reason": "ok"},
        "citation_quality": {"score": 8, "reason": "ok"},
        "comprehensiveness": {"score": 9, "reason": "ok"},
        "overall": {"score": 8, "reason": "ok"}
    }""")
    result = (await EvaluationRunner(judge).evaluate([sample_case()]))[0]
    assert result.judge is not None
    assert result.judge.mean == 8.4
    assert result.judge_score == 0.85
    assert result.score == pytest.approx(0.94)
    assert result.metadata["aggregation"] == {"rule": 0.6, "judge": 0.4}


def test_rule_only_composite_uses_full_available_weight():
    metrics = evaluate_rules(sample_case())
    composite = compute_composite_score(metrics)
    assert composite["rule_score"] == 1.0
    assert composite["judge_score"] is None
    assert composite["composite_score"] == 1.0


@pytest.mark.asyncio
async def test_paired_release_gate_rejects_regression():
    runner = EvaluationRunner()
    baseline = await runner.evaluate([sample_case()])
    degraded = sample_case()
    degraded.report = "仅有一段无引用内容"
    degraded.claims[0].supported = False
    degraded.claims[0].citation_ids = []
    candidate = await runner.evaluate([degraded])
    gate = runner.compare_and_gate(baseline, candidate, max_regression=0.01)
    assert gate["passed"] is False
    assert gate["mean_delta"] < 0


def test_bootstrap_and_effect_size_are_reported():
    statistics = bootstrap_ci_paired([0.1, 0.2, 0.3], n_bootstrap=200)
    assert statistics["mean_diff"] == 0.2
    assert statistics["ci_lower"] > 0
    assert cohens_d([0.8, 0.9, 1.0], [0.5, 0.6, 0.7]) > 0


def test_research_bench_and_hotpotqa_adapters():
    bench = ResearchBench()
    question = bench.get_questions(n=1)[0]
    report = "中文推理、代码生成、长上下文与技术路线。[1]"
    result = bench.evaluate_report(report, question["id"])
    assert result["composite_score"] > 0

    hotpot = HotpotQABenchmark(use_mock=True)
    assert hotpot.exact_match("The Shanghai!", "Shanghai")
    assert hotpot.f1_score("alpha beta", "alpha gamma") == 0.5
    assert hotpot.evaluate([
        {"prediction": "清朝", "gold": "清朝", "report": "曹雪芹是清朝作家"}
    ])["pass@1"] == 1.0
