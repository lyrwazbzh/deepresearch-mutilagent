from __future__ import annotations

import pytest
from sqlalchemy import func, select

from src.agent.backends.persistence.models import (
    ConversationMessageRow,
    NodeRunRow,
    ResearchCheckpointRow,
    ResearchTaskRow,
)
from src.agent.task_service import CreateTaskRequest


PLAN = {
    "hypotheses": [
        {
            "hypothesis_id": "h1",
            "claim": "Claim",
            "evidence_needed": ["Evidence"],
            "refuted_if": ["Counterexample"],
            "priority": 1,
            "queries": [{"query_id": "q1", "channel": "web", "text": "source query"}],
        }
    ],
    "deepen_history": [],
}


def create_and_run(container, key: str = "key"):
    created = container.task_service.create(
        "tenant_test", "user_test", key, CreateTaskRequest(query="question")
    )
    result = container.worker.run(created.projection.task_id)
    return created.projection.task_id, result


def test_direct_end_to_end(make_container, session_factory) -> None:
    container, runtime = make_container(
        {"routing": [{"mode": "direct"}], "reasoning": ["final answer"]}
    )
    task_id, result = create_and_run(container)
    assert result.outcome == "success"
    assert len(runtime.calls["routing"]) == 1
    assert len(runtime.calls["reasoning"]) == 1
    with session_factory() as session:
        task = session.get(ResearchTaskRow, task_id)
        assert task is not None and task.status == "completed"
        messages = session.scalars(
            select(ConversationMessageRow).where(ConversationMessageRow.task_id == task_id)
        ).all()
        assert [(message.role, message.content) for message in messages] == [
            ("user", "question"),
            ("assistant", "final answer"),
        ]
        assert session.scalar(
            select(func.count()).select_from(NodeRunRow).where(NodeRunRow.task_id == task_id)
        ) == 2
        assert session.scalar(
            select(func.count())
            .select_from(ResearchCheckpointRow)
            .where(ResearchCheckpointRow.task_id == task_id)
        ) == 3


@pytest.mark.parametrize(
    ("agent", "binding"),
    [("deep_scout", "tool"), ("data_analyst", "data"), ("code_wizard", "code")],
)
def test_focused_dispatches_to_bounded_agent(make_container, session_factory, agent, binding) -> None:
    container, runtime = make_container(
        {
            "routing": [{"mode": "focused", "target_agent": agent}],
            binding: [{"action": "final", "answer": f"{agent} answer"}],
        }
    )
    task_id, result = create_and_run(container, key=agent)
    assert result.outcome == "success"
    assert len(runtime.calls[binding]) == 1
    with session_factory() as session:
        nodes = session.scalars(
            select(NodeRunRow.node_name)
            .where(NodeRunRow.task_id == task_id)
            .order_by(NodeRunRow.created_at)
        ).all()
        assert nodes == ["route", "focused_dispatch", agent]
        assert session.get(ResearchTaskRow, task_id).status == "completed"


def test_deep_stops_after_persisted_plan_checkpoint(make_container, session_factory) -> None:
    container, _ = make_container({"routing": [{"mode": "deep"}], "reasoning": [PLAN]})
    task_id, result = create_and_run(container, key="deep")
    assert result.outcome == "phase1_deep_planned"
    with session_factory() as session:
        task = session.get(ResearchTaskRow, task_id)
        assert task is not None
        assert task.status == "running"
        assert task.latest_state["plan"]["hypotheses"][0]["hypothesis_id"] == "h1"
        nodes = set(
            session.scalars(select(NodeRunRow.node_name).where(NodeRunRow.task_id == task_id)).all()
        )
        assert nodes == {"route", "chief_plan"}
        checkpoint = session.scalar(
            select(ResearchCheckpointRow)
            .where(ResearchCheckpointRow.task_id == task_id)
            .order_by(ResearchCheckpointRow.checkpoint_seq.desc())
            .limit(1)
        )
        assert checkpoint.graph_position == "chief_plan"

