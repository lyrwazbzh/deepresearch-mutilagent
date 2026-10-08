from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from src.agent.identity import analysis_request_id, content_hash
from src.agent.schema.task import TaskBudget
from src.agent.schema.runtime import ContextPacket, ExecutionContext
from src.agent.schema.state import (
    AnalysisRequest,
    BranchResult,
    CriticResult,
    ResearchRound,
    ResearchState,
)
from src.agent.graph.control import AnalysisDecision, CoverageEvaluator
from src.agent.runtime.context import ContextBuilder


class NeverCancelled:
    def is_cancelled(self) -> bool:
        return False


class StructuredModel:
    def __init__(self, payload):
        self.payload = payload

    def invoke_structured(self, prompt, schema):
        del prompt
        return schema.model_validate(self.payload)


def state() -> ResearchState:
    data = {"value": 1}
    return ResearchState.model_validate(
        {
            "tenant_id": "tenant",
            "user_id": "user",
            "thread_id": "thread",
            "task_id": "task",
            "query": "question",
            "route": {"mode": "deep"},
            "plan": {
                "hypotheses": [
                    {
                        "hypothesis_id": "h1",
                        "claim": "claim",
                        "evidence_needed": ["source"],
                        "refuted_if": ["counter"],
                        "priority": 1,
                        "queries": [
                            {
                                "query_id": "q0",
                                "channel": "web",
                                "text": "original",
                            },
                            {
                                "query_id": "q1",
                                "channel": "rag",
                                "text": "deeper",
                            },
                        ],
                    }
                ],
                "deepen_history": [
                    {
                        "depth": 1,
                        "reason": "need a document",
                        "added_query_ids": ["q1"],
                    }
                ],
            },
            "execution": {"depth": 1, "max_depth": 3},
            "research_rounds": {
                "0": {
                    "web": {"status": "completed"},
                    "rag": {"status": "skipped"},
                    "sql": {"status": "skipped"},
                },
                "1": {
                    "web": {"status": "skipped"},
                    "rag": {"status": "completed"},
                    "sql": {"status": "skipped"},
                },
            },
            "evidence_pool": [
                {
                    "evidence_id": "e1",
                    "hypothesis_id": "h1",
                    "summary": "summary",
                    "excerpt": "excerpt",
                    "source_type": "web",
                    "stance": "support",
                    "relevance": 0.9,
                    "data": data,
                    "source_uri": "https://example.test",
                    "retrieved_at": datetime.now(timezone.utc),
                    "content_hash": content_hash(data),
                }
            ],
        }
    )


def packet() -> ContextPacket:
    return ContextPacket(
        role="analysis_decision",
        task_id="task",
        query="question",
        deadline=datetime.now(timezone.utc) + timedelta(minutes=1),
    )


def execution() -> ExecutionContext:
    return ExecutionContext(
        tenant_id="tenant",
        user_id="user",
        task_id="task",
        thread_id="thread",
        lease_token="lease",
        cancellation_token=NeverCancelled(),
        deadline=datetime.now(timezone.utc) + timedelta(minutes=1),
    )


def test_analysis_identity_is_order_independent_and_reentry_is_noop() -> None:
    first = analysis_request_id("task", "h1", " objective ", ["e2", "e1"], " output ")
    second = analysis_request_id("task", "h1", "objective", ["e1", "e2"], "output")
    assert first == second

    current = state()
    objective = "Compare values"
    expected = "A comparison table"
    identity = analysis_request_id("task", "h1", objective, ["e1"], expected)
    current.analysis_requests.append(
        AnalysisRequest(
            analysis_id=identity,
            hypothesis_id="h1",
            objective=objective,
            input_evidence_ids=["e1"],
            expected_output=expected,
            status="completed",
        )
    )
    model = StructuredModel(
        {
            "requests": [
                {
                    "hypothesis_id": "h1",
                    "objective": objective,
                    "input_evidence_ids": ["e1"],
                    "expected_output": expected,
                }
            ]
        }
    )

    result = AnalysisDecision().run(packet(), model, current)

    assert result.patch.analysis_request_appends == []


def test_analysis_decision_rejects_unknown_evidence() -> None:
    model = StructuredModel(
        {
            "requests": [
                {
                    "hypothesis_id": "h1",
                    "objective": "compute",
                    "input_evidence_ids": ["unknown"],
                    "expected_output": "table",
                }
            ]
        }
    )
    with pytest.raises(ValueError, match="unknown Evidence"):
        AnalysisDecision().run(packet(), model, state())


def test_coverage_checks_each_query_at_its_originating_depth() -> None:
    result = CoverageEvaluator().evaluate(state(), TaskBudget())
    assert result.complete
    assert result.gaps == []


def test_coverage_is_structural_and_stops_at_max_depth() -> None:
    current = state()
    current.research_rounds["1"] = ResearchRound(
        web=BranchResult(status="skipped"),
        rag=BranchResult(status="failed"),
        sql=BranchResult(status="skipped"),
    )
    current.execution.max_depth = 1

    result = CoverageEvaluator().evaluate(current, TaskBudget())

    assert not result.complete
    assert result.stop_reason == "max_depth"
    assert result.gaps[0].gap_type == "missing_channel"


def test_critic_contract_requires_actionable_gaps_and_score_threshold() -> None:
    with pytest.raises(ValidationError):
        CriticResult(score=7, decision="pass")
    with pytest.raises(ValidationError):
        CriticResult(score=7, decision="retrieve", gaps=[])
    with pytest.raises(ValidationError):
        CriticResult(
            score=7,
            decision="retrieve",
            gaps=[{"hypothesis_id": "h1", "reason": "missing"}],
        )
    assert CriticResult(score=8, decision="pass").decision == "pass"


def test_context_builder_scopes_deepen_and_critic_retrieval_assignments() -> None:
    current = state()
    builder = ContextBuilder()
    deepen_packet = builder.build(
        "deep_scout_rag", current, execution(), TaskBudget()
    )
    assert [item["query_id"] for item in deepen_packet.payload["assignments"][0]["queries"]] == [
        "q1"
    ]

    current.critic = CriticResult.model_validate(
        {
            "score": 7,
            "decision": "retrieve",
            "gaps": [
                {
                    "hypothesis_id": "h1",
                    "reason": "Find a primary source",
                    "target_channel": "web",
                }
            ],
        }
    )
    critic_packet = builder.build(
        "deep_scout_web", current, execution(), TaskBudget()
    )
    query = critic_packet.payload["assignments"][0]["queries"][0]
    assert query["query_id"].startswith("critic_")
    assert query["text"] == "Find a primary source"
