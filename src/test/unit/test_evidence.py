from __future__ import annotations

from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from src.agent.identity import content_hash
from src.agent.schema.state import AdoptedResult, BranchResult, Evidence, ResearchState
from src.agent.graph.evidence import BranchNotTerminalError, EvidenceConsistencyError, EvidencePipeline


class NoWorkspace:
    def metadata(self, *_args):
        raise AssertionError("workspace lookup was not expected")


class NoDocuments:
    def verify_chunk_hash(self, *_args):
        raise AssertionError("document lookup was not expected")


def state_with(items, *, status="completed"):
    return ResearchState.model_validate(
        {
            "tenant_id": "t",
            "user_id": "u",
            "thread_id": "thread",
            "task_id": "task",
            "query": "q",
            "route": {"mode": "deep"},
            "plan": {
                "hypotheses": [
                    {
                        "hypothesis_id": "h1",
                        "claim": "claim",
                        "evidence_needed": ["fact"],
                        "refuted_if": ["counter"],
                        "priority": 1,
                        "queries": [{"query_id": "q1", "channel": "sql", "text": "x"}],
                    },
                    {
                        "hypothesis_id": "h2",
                        "claim": "claim2",
                        "evidence_needed": ["fact"],
                        "refuted_if": ["counter"],
                        "priority": 2,
                        "queries": [{"query_id": "q2", "channel": "sql", "text": "x"}],
                    },
                ]
            },
            "research_rounds": {
                "0": {
                    "web": {"status": "skipped"},
                    "rag": {"status": "skipped"},
                    "sql": {"status": status, "items": items},
                }
            },
        }
    )


def adopted(hypothesis_id="h1", result_key="a" * 64):
    data = {"columns": ["value"], "rows": [[1]], "truncated": False}
    return {
        "hypothesis_id": hypothesis_id,
        "result_key": result_key,
        "summary": "one row",
        "excerpt": "value=1",
        "stance": "support",
        "relevance": 0.9,
        "content_hash": content_hash(data),
        "data": data,
    }


def test_branch_status_and_adopted_contract() -> None:
    assert BranchResult(status="skipped").model_dump()["status"] == "skipped"
    with pytest.raises(ValidationError):
        BranchResult(status="pending", items=[adopted()])
    with pytest.raises(ValidationError):
        AdoptedResult(**{**adopted(), "stance": "maybe"})
    with pytest.raises(ValidationError):
        AdoptedResult(**{**adopted(), "relevance": 1.1})
    with pytest.raises(ValidationError):
        AdoptedResult(**{**adopted(), "workspace_path": "files/x"})


def test_evidence_location_and_web_source_contract() -> None:
    with pytest.raises(ValidationError):
        Evidence(
            evidence_id="e",
            hypothesis_id="h",
            summary="s",
            excerpt="x",
            source_type="web",
            stance="support",
            relevance=1,
            data={"x": 1},
            content_hash="a" * 64,
        )
    value = Evidence(
        evidence_id="e",
        hypothesis_id="h",
        summary="s",
        excerpt="x",
        source_type="web",
        stance="support",
        relevance=1,
        data={"x": 1},
        source_uri="https://example.test/",
        retrieved_at=datetime.now(timezone.utc),
        content_hash="a" * 64,
    )
    assert value.source_type == "web"


def test_merge_identity_replay_and_hypothesis_scope() -> None:
    pipeline = EvidencePipeline(NoWorkspace(), NoDocuments())
    first = pipeline.merge(state_with([adopted(), adopted("h2")]))
    assert len(first) == 2
    assert first[0].content_hash == first[1].content_hash
    assert first[0].evidence_id != first[1].evidence_id

    replay_state = state_with([adopted(), adopted("h2")])
    replay_state.evidence_pool = first
    assert pipeline.merge(replay_state) == first

    independent = state_with([adopted(), adopted(result_key="b" * 64)])
    assert len(pipeline.merge(independent)) == 2


def test_merge_rejects_pending_hash_mismatch_and_conflicting_replay() -> None:
    pipeline = EvidencePipeline(NoWorkspace(), NoDocuments())
    with pytest.raises(BranchNotTerminalError):
        pipeline.merge(state_with([], status="pending"))
    bad = adopted()
    bad["content_hash"] = "0" * 64
    with pytest.raises(EvidenceConsistencyError, match="content_hash"):
        pipeline.merge(state_with([bad]))

    state = state_with([adopted()])
    evidence = pipeline.merge(state)[0]
    evidence.summary = "conflict"
    state.evidence_pool = [evidence]
    with pytest.raises(EvidenceConsistencyError, match="immutable"):
        pipeline.merge(state)
