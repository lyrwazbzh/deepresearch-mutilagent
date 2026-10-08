from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import func, select

from src.agent.schema.task import TaskBudget
from src.agent.schema.runtime import ExecutionContext, NodeResult, StatePatch
from src.agent.schema.state import ResearchState
from src.agent.backends.persistence.models import (
    NodeRunRow,
    OutboxMessageRow,
    ResearchCheckpointRow,
    ResearchEventRow,
    ResearchTaskRow,
    utcnow,
)
from src.agent.runtime.state_manager import StateManager
from src.agent.errors import LeaseLostError
from src.agent.task_service import CreateTaskRequest
from src.agent.workers.research import DatabaseCancellationToken


def setup_running(container, session_factory):
    created = container.task_service.create(
        "tenant_test", "user_test", "node-key", CreateTaskRequest(query="question")
    )
    task, token = container.worker.claim(created.projection.task_id)
    with session_factory() as session:
        checkpoint = session.scalar(
            select(ResearchCheckpointRow).where(ResearchCheckpointRow.task_id == task.task_id)
        )
    execution = ExecutionContext(
        tenant_id=task.tenant_id,
        user_id=task.user_id,
        task_id=task.task_id,
        thread_id=task.thread_id,
        lease_token=token,
        cancellation_token=DatabaseCancellationToken(session_factory, task.task_id),
        deadline=utcnow() + timedelta(minutes=5),
    )
    return task, execution, checkpoint.checkpoint_id


def test_completed_node_replays_without_handler(make_container, session_factory) -> None:
    container, _ = make_container({})
    task, execution, checkpoint_id = setup_running(container, session_factory)
    calls = 0

    def handler(_packet, _attempt):
        nonlocal calls
        calls += 1
        return NodeResult(patch=StatePatch(values={"route": {"mode": "direct"}, "plan": None}))

    kwargs = dict(
        node_name="route",
        node_key="route",
        context_role="route",
        input_checkpoint_id=checkpoint_id,
        execution=execution,
        state=ResearchState.model_validate(task.latest_state),
        budget=TaskBudget.model_validate(task.task_budget),
        handler=handler,
    )
    first = container.node_runner.execute(**kwargs)
    second = container.node_runner.execute(**kwargs)
    assert first.replayed is False
    assert second.replayed is True
    assert first.node_run_id == second.node_run_id
    assert calls == 1
    with session_factory() as session:
        assert session.get(NodeRunRow, first.node_run_id).attempt_count == 1


def test_failed_node_retry_reuses_semantic_identity_and_increments_attempt(
    make_container, session_factory
) -> None:
    container, _ = make_container({})
    task, execution, checkpoint_id = setup_running(container, session_factory)
    calls = 0

    def handler(_packet, attempt):
        nonlocal calls
        calls += 1
        if attempt == 1:
            raise RuntimeError("transient node failure")
        return NodeResult(
            patch=StatePatch(values={"route": {"mode": "direct"}, "plan": None})
        )

    with pytest.raises(RuntimeError, match="transient node failure"):
        container.node_runner.execute(
            node_name="route",
            node_key="route:stable",
            context_role="route",
            input_checkpoint_id=checkpoint_id,
            execution=execution,
            state=ResearchState.model_validate(task.latest_state),
            budget=TaskBudget.model_validate(task.task_budget),
            handler=handler,
        )
    completed = container.node_runner.execute(
        node_name="route",
        node_key="route:stable",
        context_role="route",
        input_checkpoint_id=checkpoint_id,
        execution=execution,
        state=ResearchState.model_validate(task.latest_state),
        budget=TaskBudget.model_validate(task.task_budget),
        handler=handler,
    )
    assert calls == 2
    assert completed.node_attempt_no == 2
    with session_factory() as session:
        row = session.scalar(
            select(NodeRunRow).where(NodeRunRow.node_key == "route:stable")
        )
        assert row.node_run_id == completed.node_run_id
        assert row.status == "completed" and row.attempt_count == 2


def test_stale_lease_cannot_commit(make_container, session_factory) -> None:
    container, _ = make_container({})
    task, execution, checkpoint_id = setup_running(container, session_factory)
    with session_factory() as session, session.begin():
        node = NodeRunRow(
            node_run_id="node_stale",
            tenant_id=task.tenant_id,
            task_id=task.task_id,
            node_key="route-stale",
            node_name="route",
            input_checkpoint_id=checkpoint_id,
            status="running",
            attempt_count=1,
        )
        session.add(node)
        row = session.get(ResearchTaskRow, task.task_id)
        row.lease_token = "new-token"
    with pytest.raises(LeaseLostError):
        container.state_manager.commit_node(
            "node_stale",
            StatePatch(values={"route": {"mode": "direct"}, "plan": None}),
            execution.lease_token,
        )


def test_commit_failure_rolls_back_state_checkpoint_event_and_outbox(
    make_container, session_factory
) -> None:
    container, _ = make_container({})
    task, execution, checkpoint_id = setup_running(container, session_factory)
    with session_factory() as session, session.begin():
        session.add(
            NodeRunRow(
                node_run_id="node_rollback",
                tenant_id=task.tenant_id,
                task_id=task.task_id,
                node_key="route-rollback",
                node_name="route",
                input_checkpoint_id=checkpoint_id,
                status="running",
                attempt_count=1,
            )
        )
    with session_factory() as session:
        counts_before = tuple(
            session.scalar(select(func.count()).select_from(model))
            for model in (ResearchCheckpointRow, ResearchEventRow, OutboxMessageRow)
        )

    def fail(step: str) -> None:
        if step == "event_added":
            raise RuntimeError("injected")

    manager = StateManager(session_factory, failpoint=fail)
    with pytest.raises(RuntimeError, match="injected"):
        manager.commit_node(
            "node_rollback",
            StatePatch(values={"route": {"mode": "direct"}, "plan": None}),
            execution.lease_token,
        )
    with session_factory() as session:
        row = session.get(ResearchTaskRow, task.task_id)
        assert row.latest_state["route"] is None
        counts_after = tuple(
            session.scalar(select(func.count()).select_from(model))
            for model in (ResearchCheckpointRow, ResearchEventRow, OutboxMessageRow)
        )
        assert counts_after == counts_before
        assert session.get(NodeRunRow, "node_rollback").status == "running"
