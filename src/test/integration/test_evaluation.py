from __future__ import annotations

from types import SimpleNamespace

from sqlalchemy import select

from src.agent.tools.documents import InMemoryDocumentIndex
from src.agent.config import Settings
from src.agent.container import build_container
from src.agent.identity import content_hash
from src.agent.schema.report import ReportAst
from src.agent.schema.state import Evidence, ResearchState
from src.agent.evaluation.research_evaluation import (
    BaseSuite,
    E2ECaseOutput,
    E2EResearchEvalSuite,
    EvalRunner,
    FactualVerdict,
    FaithfulnessVerdict,
    ImmutableDataset,
    JudgeError,
    RAGEvalSuite,
    ResearchLTMEvalSuite,
)
from src.agent.backends.events import InMemoryEventBroker
from src.agent.backends.persistence.models import (
    DocumentChunkRow,
    EvalCaseResultRow,
    ResearchMemoryRow,
    ResearchTaskRow,
)
from src.agent.task_service import CreateTaskRequest
from src.agent.backends.workspace import InMemoryObjectStore
from src.test.harness import DeterministicIdFactory, TestClock


def _real_runtime_container(session_factory):
    settings = Settings(
        database_url="sqlite://",
        redis_url="redis://unused",
        celery_broker_url="redis://unused",
        default_tenant_id="tenant_test",
        default_user_id="user_test",
        model_mode="fake",
        phase2_enabled=True,
    )
    return build_container(
        settings,
        session_factory=session_factory,
        event_broker=InMemoryEventBroker(),
        workspace_store=InMemoryObjectStore(),
        document_store=InMemoryObjectStore(),
        document_index=InMemoryDocumentIndex(),
    )


def test_rag_eval_compares_bm25_baseline_and_production_retrieval(session_factory) -> None:
    container = _real_runtime_container(session_factory)
    doc_id = container.document_service.create(
        tenant_id="tenant_test",
        name="phase12.txt",
        media_type="text/plain",
        data=b"OpenTelemetry makes research runtime behavior observable and diagnosable.",
        doc_id="doc_phase12_eval",
    )
    assert container.document_worker.run(doc_id) == 1
    with session_factory() as session:
        relevant = list(
            session.scalars(
                select(DocumentChunkRow.chunk_id).where(
                    DocumentChunkRow.doc_id == doc_id,
                    DocumentChunkRow.parent_id.is_not(None),
                )
            )
        )
        embedding = container.model_resolver.resolve_snapshot(
            session, "tenant_test", ("embedding",)
        ).get("embedding")
        rerank = container.model_resolver.resolve_snapshot(
            session, "tenant_test", ("rerank",)
        ).get("rerank")
    case = {
        "case_id": "rag_observability",
        "tenant_id": "tenant_test",
        "query": "observable research runtime",
        "doc_ids": [doc_id],
        "relevant_chunk_ids": relevant,
    }
    dataset = ImmutableDataset(suite="rag", version="v1", cases=(case,))
    current = RAGEvalSuite(
        container.retrieval_gateway,
        embedding,
        rerank,
        retrieval_config="current",
    )
    baseline = RAGEvalSuite(
        container.retrieval_gateway,
        embedding,
        rerank,
        retrieval_config="bm25_baseline",
    )
    current.preflight(dataset, {})
    assert current.execute_case(case, "scope", SimpleNamespace()).metrics["recall_at_5"] == 1
    assert baseline.execute_case(case, "scope", SimpleNamespace()).metrics["recall_at_5"] == 1


def test_ltm_eval_uses_memory_service_and_enforces_zero_scope_leakage(
    make_container, session_factory
) -> None:
    container, _ = make_container({})
    created = container.task_service.create(
        "tenant_test",
        "user_test",
        "phase12-ltm",
        CreateTaskRequest(query="Remember that I prefer primary papers."),
    )
    message_id = created.projection.message_id
    assert message_id is not None
    dataset = ImmutableDataset(
        suite="research_ltm",
        version="v1",
        cases=(
            {
                "case_id": "write_true",
                "case_kind": "write_gate",
                "tenant_id": "tenant_test",
                "user_id": "user_test",
                "candidate": {
                    "topic": "source preference",
                    "category": "research_preference",
                    "content": "Prefer primary papers.",
                    "confidence": 0.95,
                    "supporting_user_message_ids": [message_id],
                },
                "user_messages": {
                    message_id: "Remember that I prefer primary papers."
                },
                "should_write": True,
                "logical_memory_id": "primary_papers",
            },
            {
                "case_id": "write_false",
                "case_kind": "write_gate",
                "tenant_id": "tenant_test",
                "user_id": "user_test",
                "candidate": {
                    "topic": "output language",
                    "category": "output_preference",
                    "content": "Use German.",
                    "confidence": 0.10,
                    "supporting_user_message_ids": [message_id],
                },
                "user_messages": {message_id: "I did not request German output."},
                "should_write": False,
            },
            {
                "case_id": "recall",
                "case_kind": "recall",
                "tenant_id": "tenant_test",
                "user_id": "user_test",
                "query": "primary papers source preference",
                "relevant_memory_ids": ["primary_papers"],
                "leakage_tenant_id": "tenant_test",
                "leakage_user_id": "other_user",
            },
        ),
    )
    suite = ResearchLTMEvalSuite(container.memory_service, session_factory)
    runner = EvalRunner(
        session_factory,
        id_factory=DeterministicIdFactory("ltm"),
        now=TestClock().now,
    )
    run = runner.run(
        suite,
        dataset,
        config_snapshot={"memory_topic_registry": "memory-topics-v1"},
        code_revision="phase12-test",
    )
    assert run.status == "completed"
    assert run.metrics_summary["write_gate_f1"] == 1
    assert run.metrics_summary["recall_at_5"] == 1
    assert run.metrics_summary["cross_tenant_user_leakage"] == 0
    with session_factory() as session:
        assert session.scalars(select(ResearchMemoryRow)).all() == []
        assert len(session.scalars(select(EvalCaseResultRow)).all()) == 3


def _report() -> ReportAst:
    return ReportAst.model_validate(
        {
            "report_version": 1,
            "title": "Phase 12",
            "sections": [
                {
                    "section_id": "s1",
                    "heading": "Finding",
                    "level": 2,
                    "blocks": [
                        {
                            "type": "paragraph",
                            "segments": [
                                {
                                    "type": "claim",
                                    "claim_id": "claim_1",
                                    "text": "The runtime is observable.",
                                    "citation_ids": ["citation_1"],
                                }
                            ],
                        }
                    ],
                }
            ],
            "citations": [{"citation_id": "citation_1", "evidence_id": "evidence_1"}],
        }
    )


class CorrectJudge:
    def evaluate(self, query, gold_facts, claims):
        del query, gold_facts
        return [
            FactualVerdict(
                claim_id=item.claim_id,
                verdict="correct",
                matched_gold_fact_ids=["fact_1"],
                reason="matches gold",
            )
            for item in claims
        ]


class SupportedJudge:
    def evaluate(self, claims, evidence_by_citation):
        assert "citation_1" in evidence_by_citation
        return [
            FaithfulnessVerdict(
                claim_id=item.claim_id,
                supported=True,
                supporting_citation_ids=list(item.citation_ids),
                reason="supported",
            )
            for item in claims
        ]


def test_e2e_eval_report_denominator_judges_manifest_and_reproducibility(
    make_container, session_factory
) -> None:
    container, _ = make_container({})
    created = container.task_service.create(
        "tenant_test", "user_test", "phase12-e2e", CreateTaskRequest(query="q")
    )
    with session_factory() as session:
        task = session.get(ResearchTaskRow, created.projection.task_id)
        state = ResearchState.model_validate(task.latest_state)
    data = {"statement": "The runtime is observable."}
    state.evidence_pool = [
        Evidence(
            evidence_id="evidence_1",
            hypothesis_id="hypothesis_1",
            summary="runtime evidence",
            excerpt="observable",
            source_type="sql",
            stance="support",
            relevance=1,
            data=data,
            content_hash=content_hash(data),
        )
    ]
    cleanup_calls: list[str] = []

    def execute(case, scope, manifest):
        manifest.add("tasks", f"{scope}:{case['case_id']}")
        if case["case_kind"] == "route_boundary":
            return E2ECaseOutput(
                task_status="completed",
                report=None,
                state=None,
                diagnostics={"route_mode": "deep"},
            )
        return E2ECaseOutput(
            task_status="completed",
            report=_report(),
            state=state,
            evidence_by_citation={"citation_1": data},
            diagnostics={"task_id": created.projection.task_id},
        )

    dataset = ImmutableDataset(
        suite="e2e",
        version="v1",
        cases=(
            {
                "case_id": "quality_1",
                "case_kind": "report_quality",
                "tenant_id": "tenant_test",
                "user_id": "user_test",
                "query": "q",
                "gold_facts": [{"fact_id": "fact_1", "statement": "observable"}],
            },
            {
                "case_id": "route_1",
                "case_kind": "route_boundary",
                "expected_diagnostics": {"route_mode": "deep"},
            },
        ),
    )
    suite = E2EResearchEvalSuite(
        execute,
        container.graph.citation_validator,
        CorrectJudge(),
        SupportedJudge(),
        cleanup=lambda _scope, manifest: cleanup_calls.extend(manifest.tasks),
    )
    run = EvalRunner(
        session_factory,
        id_factory=DeterministicIdFactory("e2e"),
        now=TestClock().now,
    ).run(
        suite,
        dataset,
        config_snapshot={
            "judge_binding": "judge-v1",
            "factual_prompt": "factual-v1",
            "faithfulness_prompt": "faithfulness-v1",
            "rubric": "rubric-v1",
        },
        code_revision="commit-abc",
    )
    assert run.dataset_ref == dataset.dataset_ref
    assert run.metrics_summary["report_quality_case_count"] == 1
    assert run.metrics_summary["diagnostic_case_count"] == 1
    assert run.metrics_summary["factual_accuracy"] == 1
    assert run.metrics_summary["citation_coverage"] == 1
    assert run.metrics_summary["completion_rate"] == 1
    assert run.metrics_summary["faithfulness"] == 1
    assert run.metrics_summary["gate_eligible"] is True
    assert len(cleanup_calls) == 2


def test_judge_error_is_null_and_run_is_not_gate_eligible(session_factory) -> None:
    class BrokenJudgeSuite(BaseSuite):
        name = "e2e"

        def execute_case(self, case, eval_scope_id, manifest):
            del case, eval_scope_id, manifest
            raise JudgeError(
                "structured_output_invalid", ("factual_accuracy", "faithfulness")
            )

        def summarize(self, results):
            return {"case_count": len(results)}

    dataset = ImmutableDataset(
        suite="e2e",
        version="judge-error-v1",
        cases=({"case_id": "judge_error_1"},),
    )
    run = EvalRunner(
        session_factory, id_factory=DeterministicIdFactory("judge")
    ).run(
        BrokenJudgeSuite(),
        dataset,
        config_snapshot={
            "judge_binding": "fixed",
            "prompt_version": "v1",
            "rubric_version": "v1",
        },
        code_revision="commit-judge",
    )
    assert run.status == "completed"
    assert run.metrics_summary["judge_error_count"] == 1
    assert run.metrics_summary["gate_eligible"] is False
    with session_factory() as session:
        result = session.get(EvalCaseResultRow, (run.eval_run_id, "judge_error_1"))
        assert result.outcome == "judge_error"
        assert result.metrics == {"factual_accuracy": None, "faithfulness": None}
