from __future__ import annotations

import pytest
from pydantic import ValidationError

from src.agent.schema.task import TaskStatus
from src.agent.schema.runtime import ContextPacket
from src.agent.schema.state import HypothesisPlan, ResearchState, RouteDecision


def test_route_decision_contract() -> None:
    assert RouteDecision(mode="focused", target_agent="deep_scout").mode == "focused"
    with pytest.raises(ValidationError):
        RouteDecision(mode="focused")
    with pytest.raises(ValidationError):
        RouteDecision(mode="direct", target_agent="code_wizard")


def test_hypothesis_plan_contract() -> None:
    plan = HypothesisPlan.model_validate(
        {
            "hypotheses": [
                {
                    "hypothesis_id": "h1",
                    "claim": "Claim",
                    "evidence_needed": ["Evidence"],
                    "refuted_if": ["Counterexample"],
                    "priority": 1,
                    "queries": [{"query_id": "q1", "channel": "web", "text": "query"}],
                }
            ]
        }
    )
    assert plan.hypotheses[0].queries[0].channel == "web"
    with pytest.raises(ValidationError):
        HypothesisPlan.model_validate(
            {
                "hypotheses": [
                    {
                        "hypothesis_id": "h1",
                        "claim": "Claim",
                        "evidence_needed": ["Evidence"],
                        "refuted_if": ["Counterexample"],
                        "priority": 1,
                        "queries": [{"query_id": "q1", "channel": "compute", "text": "x"}],
                    }
                ]
            }
        )


def test_research_state_and_context_packet_do_not_duplicate_runtime_state() -> None:
    state = ResearchState(
        tenant_id="t", user_id="u", thread_id="th", task_id="task", query="question"
    )
    dumped = state.model_dump()
    assert "task_status" not in dumped
    assert "task_budget" not in dumped
    assert "lease_token" not in dumped
    assert "remaining_budget" not in dumped
    assert "research_state" not in ContextPacket.model_fields


def test_task_status_is_exactly_the_documented_eight_states() -> None:
    assert {item.value for item in TaskStatus} == {
        "pending",
        "running",
        "pausing",
        "paused",
        "cancelling",
        "cancelled",
        "completed",
        "failed",
    }

