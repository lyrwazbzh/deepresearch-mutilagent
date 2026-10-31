import logging

import pytest

from service.deep_research_v2.agents.critic import CriticMaster
from service.deep_research_v2.state import (
    ResearchPhase,
    create_initial_state,
)


def _critic_with_result(result):
    critic = object.__new__(CriticMaster)
    critic.name = "CriticMaster"
    critic.logger = logging.getLogger("test.critic")

    async def review(_state):
        return result

    critic._review_content = review
    return critic


def _review(*, score, verdict="pass", issues=None, missing=None, fact_checks=None):
    return {
        "overall_assessment": {
            "quality_score": score,
            "verdict": verdict,
            "summary": "reviewed",
        },
        "issues": issues or [],
        "missing_aspects": missing or [],
        "fact_check_results": fact_checks or [],
    }


def _state(*, iteration=0, max_iterations=3):
    state = create_initial_state("储能行业研究", "session-1")
    state["phase"] = ResearchPhase.REVIEWING.value
    state["iteration"] = iteration
    state["max_iterations"] = max_iterations
    state["final_report"] = "report"
    return state


@pytest.mark.asyncio
async def test_low_score_cannot_pass_even_when_model_says_pass():
    state = _state()
    critic = _critic_with_result(_review(score=6.9, verdict="pass"))

    await critic.process(state)

    assert state["phase"] == ResearchPhase.REVISING.value
    assert state["quality_gate_passed"] is False
    assert "quality_score_below_threshold" in state["quality_gate_reasons"]
    assert state["review_history"][0]["next_action"] == "revise"


@pytest.mark.asyncio
async def test_blocking_issue_cannot_pass_at_high_score():
    state = _state()
    critic = _critic_with_result(_review(
        score=9,
        verdict="pass",
        issues=[{
            "issue_type": "hallucination",
            "severity": "major",
            "description": "unsupported claim",
            "suggestion": "remove it",
        }],
    ))

    await critic.process(state)

    assert state["phase"] == ResearchPhase.REVISING.value
    assert "blocking_issues_present" in state["quality_gate_reasons"]


@pytest.mark.asyncio
async def test_valid_review_is_the_only_normal_completion_path():
    state = _state()
    critic = _critic_with_result(_review(score=8, verdict="pass"))

    await critic.process(state)

    assert state["phase"] == ResearchPhase.COMPLETED.value
    assert state["quality_gate_passed"] is True
    assert state["quality_gate_reasons"] == []


@pytest.mark.asyncio
async def test_invalid_review_fails_closed_at_last_round():
    state = _state(iteration=2, max_iterations=3)
    critic = _critic_with_result({})

    await critic.process(state)

    assert state["phase"] == ResearchPhase.QUALITY_GATE_FAILED.value
    assert state["quality_gate_passed"] is False
    assert "invalid_critic_response" in state["quality_gate_reasons"]
    assert state["iteration"] == 3
    assert state["review_history"][-1]["next_action"] == "quality_gate_failed"


@pytest.mark.asyncio
async def test_malformed_issue_cannot_bypass_gate():
    state = _state()
    critic = _critic_with_result(_review(
        score=9,
        issues=[{"issue_type": "hallucination", "description": "unsupported"}],
    ))

    await critic.process(state)

    assert state["quality_gate_passed"] is False
    assert "invalid_critic_response" in state["quality_gate_reasons"]


@pytest.mark.asyncio
async def test_unverified_fact_forces_new_research():
    state = _state()
    critic = _critic_with_result(_review(
        score=9,
        fact_checks=[{
            "fact_id": "fact-1",
            "status": "unverified",
            "reason": "缺少一手统计来源",
        }],
    ))

    await critic.process(state)

    assert state["phase"] == ResearchPhase.RE_RESEARCHING.value
    assert "fact_check_failures_present" in state["quality_gate_reasons"]
    assert state["pending_search_queries"] == ["缺少一手统计来源"]
