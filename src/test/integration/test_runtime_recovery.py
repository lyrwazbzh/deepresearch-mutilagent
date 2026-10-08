from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select

from src.agent.schema.task import TaskBudget
from src.agent.schema.runtime import ExecutionContext, StatePatch
from src.agent.schema.state import Evidence
from src.agent.schema.model_config import ModelBindingSnapshot
from src.agent.llm.runtime import ModelCallError, ModelRuntime, ProviderChatResponse
from src.agent.backends.persistence.models import (
    NodeRunRow,
    ModelPricingRow,
    OutboxMessageRow,
    ResearchCheckpointRow,
    ResearchTaskCommandRow,
    ResearchTaskRow,
    TaskKnowledgeGraphRow,
    utcnow,
)
from src.agent.runtime.budget import (
    BudgetDelta,
    BudgetExceededError,
    TaskBudgetManager,
    bind_runtime_effect_context,
)
from src.agent.errors import IdempotencyConflictError
from src.agent.task_service import CreateTaskRequest
from src.agent.workers.recovery import RuntimeRecoveryScanner
from src.agent.workers.outbox import CeleryJobPublisher


def _create(make_container, key: str = "phase9-runtime"):
    container, _ = make_container({})
    created = container.task_service.create(
        "tenant_test", "user_test", key, CreateTaskRequest(query="runtime test")
    )
    return container, created.projection.task_id


def _node(session_factory, task_id: str, node_id: str = "node_phase9") -> NodeRunRow:
    with session_factory() as session, session.begin():
        checkpoint_id = session.scalar(
            select(OutboxMessageRow.aggregate_id).where(
                OutboxMessageRow.kind == "research.schedule",
                OutboxMessageRow.aggregate_id == task_id,
            )
        )
        del checkpoint_id
        row = NodeRunRow(
            node_run_id=node_id,
            tenant_id="tenant_test",
            task_id=task_id,
            node_key=node_id,
            node_name="route",
            input_checkpoint_id="ckpt_input",
            status="running",
            attempt_count=1,
        )
        session.add(row)
        return row


def test_checkpoint_resume_after_route_does_not_run_route_again(
    make_container, session_factory
) -> None:
    container, runtime = make_container(
        {"reasoning:chief_direct": ["resumed direct response"]}
    )
    container.graph.phase2_enabled = True
    created = container.task_service.create(
        "tenant_test",
        "user_test",
        "phase9-direct-resume",
        CreateTaskRequest(query="resume direct"),
    )
    task_id = created.projection.task_id
    _, lease = container.worker.claim(task_id)
    with session_factory() as session, session.begin():
        checkpoint = session.scalar(
            select(ResearchCheckpointRow)
            .where(ResearchCheckpointRow.task_id == task_id)
            .order_by(ResearchCheckpointRow.checkpoint_seq.desc())
            .limit(1)
        )
        assert checkpoint is not None
        route_run = NodeRunRow(
            node_run_id="node_phase9_resume_route",
            tenant_id="tenant_test",
            task_id=task_id,
            node_key="route",
            node_name="route",
            input_checkpoint_id=checkpoint.checkpoint_id,
            status="running",
            attempt_count=1,
        )
        session.add(route_run)
    container.state_manager.commit_node(
        route_run.node_run_id,
        StatePatch(values={"route": {"mode": "direct", "target_agent": None}}),
        lease,
        output={"route": {"mode": "direct", "target_agent": None}},
    )
    with session_factory() as session, session.begin():
        task = session.get(ResearchTaskRow, task_id)
        assert task is not None
        task.status = "pending"
        task.lease_token = None
        task.leased_until = None
        task.budget_accounted_at = None

    result = container.worker.run(task_id)

    assert result.outcome == "success"
    assert runtime.calls["routing"] == []
    with session_factory() as session:
        nodes = session.scalars(
            select(NodeRunRow)
            .where(NodeRunRow.task_id == task_id)
            .order_by(NodeRunRow.created_at)
        ).all()
        assert [row.node_name for row in nodes] == ["route", "chief_direct"]


def test_task_commands_are_idempotent_and_schedule_resume(
    make_container, session_factory
) -> None:
    container, task_id = _create(make_container, "phase9-commands")

    paused = container.task_service.command(
        "tenant_test", "user_test", task_id, "pause", "command-1"
    )
    replay = container.task_service.command(
        "tenant_test", "user_test", task_id, "pause", "command-1"
    )
    assert paused.task_status.value == "paused"
    assert replay == paused
    with pytest.raises(IdempotencyConflictError):
        container.task_service.command(
            "tenant_test", "user_test", task_id, "cancel", "command-1"
        )

    resumed = container.task_service.command(
        "tenant_test", "user_test", task_id, "resume", "command-2"
    )
    cancelled = container.task_service.command(
        "tenant_test", "user_test", task_id, "cancel", "command-3"
    )
    assert resumed.task_status.value == "pending"
    assert cancelled.task_status.value == "cancelled"
    with session_factory() as session:
        assert session.scalar(
            select(ResearchTaskCommandRow).where(
                ResearchTaskCommandRow.idempotency_key == "command-2"
            )
        )
        assert session.scalar(
            select(OutboxMessageRow).where(
                OutboxMessageRow.kind == "research.schedule",
                OutboxMessageRow.idempotency_key.like("resume:%"),
            )
        )


def test_recovery_fences_expired_activation_and_settles_wall_time(
    make_container, session_factory
) -> None:
    _, task_id = _create(make_container, "phase9-recovery")
    now = utcnow()
    with session_factory() as session, session.begin():
        task = session.get(ResearchTaskRow, task_id)
        assert task is not None
        task.status = "running"
        task.lease_token = "expired-token"
        task.budget_accounted_at = now - timedelta(seconds=12)
        task.leased_until = now - timedelta(seconds=2)
        task.updated_at = now

    repaired = RuntimeRecoveryScanner(session_factory).scan_once(now=now)

    assert repaired == 1
    with session_factory() as session:
        task = session.get(ResearchTaskRow, task_id)
        assert task is not None
        assert task.status == "pending"
        assert task.lease_token is None
        budget = TaskBudget.model_validate(task.task_budget)
        assert 9_000 <= budget.usage.wall_time_ms <= 10_000
        assert session.scalar(
            select(OutboxMessageRow).where(
                OutboxMessageRow.idempotency_key
                == f"recovery:{task_id}:expired-token"
            )
        )


def test_running_pause_resume_and_cancel_converge_at_safe_boundary(
    make_container, session_factory
) -> None:
    container, task_id = _create(make_container, "phase9-running-controls")
    now = utcnow()
    with session_factory() as session, session.begin():
        task = session.get(ResearchTaskRow, task_id)
        assert task is not None
        task.status = "running"
        task.lease_token = "control-lease-1"
        task.leased_until = now + timedelta(minutes=5)
        task.budget_accounted_at = now
    requested = container.task_service.command(
        "tenant_test", "user_test", task_id, "pause", "pause-running"
    )
    assert requested.task_status.value == "pausing"
    container.worker._finish(task_id, "control-lease-1", "paused", None)
    assert container.task_service.get(
        "tenant_test", "user_test", task_id
    ).task_status.value == "paused"

    resumed = container.task_service.command(
        "tenant_test", "user_test", task_id, "resume", "resume-running"
    )
    assert resumed.task_status.value == "pending"
    with session_factory() as session, session.begin():
        task = session.get(ResearchTaskRow, task_id)
        assert task is not None
        task.status = "running"
        task.lease_token = "control-lease-2"
        task.leased_until = utcnow() + timedelta(minutes=5)
        task.budget_accounted_at = utcnow()
    cancelling = container.task_service.command(
        "tenant_test", "user_test", task_id, "cancel", "cancel-running"
    )
    assert cancelling.task_status.value == "cancelling"
    container.worker._finish(task_id, "control-lease-2", "cancelled", None)
    assert container.task_service.get(
        "tenant_test", "user_test", task_id
    ).task_status.value == "cancelled"


def test_budget_reservation_settlement_replay_and_concurrency(
    make_container, session_factory
) -> None:
    _, task_id = _create(make_container, "phase9-budget")
    _node(session_factory, task_id)
    with session_factory() as session, session.begin():
        task = session.get(ResearchTaskRow, task_id)
        assert task is not None
        payload = dict(task.task_budget)
        payload["budget"] = {**payload["budget"], "max_search_calls": 1}
        task.task_budget = payload

    manager = TaskBudgetManager(session_factory)

    def reserve(slot: int) -> str:
        key = f"op-{slot}"
        try:
            manager.reserve_effect(
                tenant_id="tenant_test",
                task_id=task_id,
                node_run_id="node_phase9",
                effect_name=f"search:{slot}",
                operation_key=key,
                upper_bound=BudgetDelta(search_calls=1),
            )
            return key
        except BudgetExceededError:
            return "exhausted"

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(reserve, [1, 2]))
    assert outcomes.count("exhausted") == 1
    winner = next(item for item in outcomes if item != "exhausted")
    manager.settle_effect(
        task_id=task_id,
        operation_key=winner,
        actual_usage=BudgetDelta(search_calls=1),
        result_kind="tool",
        result_payload={"status": "success"},
    )
    replay = manager.reserve_effect(
        tenant_id="tenant_test",
        task_id=task_id,
        node_run_id="node_phase9",
        effect_name="search:replay",
        operation_key=winner,
        upper_bound=BudgetDelta(search_calls=1),
    )
    assert replay.replay_payload == {"status": "success"}
    with session_factory() as session:
        task = session.get(ResearchTaskRow, task_id)
        assert task is not None
        assert task.task_budget["usage"]["search_calls"] == 1


def test_merge_evidence_atomically_schedules_task_kg(
    make_container, session_factory
) -> None:
    container, task_id = _create(make_container, "phase9-atomic-kg")
    container.state_manager.phase9_enabled = True
    now = utcnow()
    evidence = Evidence(
        evidence_id="ev_phase9",
        hypothesis_id="h1",
        summary="metric",
        excerpt="value is 10",
        source_type="sql",
        stance="support",
        relevance=1,
        data={"value": 10},
        content_hash="a" * 64,
    )
    with session_factory() as session, session.begin():
        task = session.get(ResearchTaskRow, task_id)
        assert task is not None
        task.status = "running"
        task.lease_token = "lease-phase9"
        task.leased_until = now + timedelta(minutes=5)
        task.budget_accounted_at = now
        session.add(
            NodeRunRow(
                node_run_id="node_merge_phase9",
                tenant_id="tenant_test",
                task_id=task_id,
                node_key="merge:phase9",
                node_name="merge_evidence",
                input_checkpoint_id="ckpt_phase9",
                status="running",
                attempt_count=1,
            )
        )

    container.state_manager.commit_node(
        "node_merge_phase9",
        StatePatch(list_appends={"evidence_pool": [evidence.model_dump(mode="json")]}),
        "lease-phase9",
    )

    with session_factory() as session:
        metadata = session.get(TaskKnowledgeGraphRow, ("tenant_test", task_id))
        assert metadata is not None and metadata.status == "building"
        outbox = session.scalar(
            select(OutboxMessageRow).where(
                OutboxMessageRow.kind == "knowledge_graph.build",
                OutboxMessageRow.aggregate_id == task_id,
            )
        )
        assert outbox is not None
        assert outbox.payload_json == {"task_id": task_id}


def test_model_budget_is_settled_once_and_replayed(
    make_container, session_factory
) -> None:
    _, task_id = _create(make_container, "phase9-model-budget")
    _node(session_factory, task_id, "node_model_phase9")
    with session_factory() as session:
        task = session.get(ResearchTaskRow, task_id)
        assert task is not None
        bindings = ModelBindingSnapshot.model_validate(task.model_binding_snapshot)
    with session_factory() as session, session.begin():
        session.add(
            ModelPricingRow(
                tenant_id="tenant_test",
                provider="fake",
                model="test",
                currency="USD",
                effective_at=utcnow() - timedelta(minutes=1),
                input_price_per_1m_tokens=Decimal("1"),
                output_price_per_1m_tokens=Decimal("2"),
            )
        )

    class Provider:
        calls = 0

        def chat(self, _config, _prompt):
            self.calls += 1
            return ProviderChatResponse(
                content="budgeted response", input_tokens=7, output_tokens=3
            )

        def embed(self, _config, _texts):
            return []

        def rerank(self, _config, _query, _candidates):
            return []

    class Token:
        def is_cancelled(self) -> bool:
            return False

    provider = Provider()
    runtime = ModelRuntime(session_factory, provider_factory=lambda _name: provider)
    model = runtime.build_chat_model_for(
        "tenant_test", bindings.get("reasoning"), "phase9_test"
    )
    execution = ExecutionContext(
        tenant_id="tenant_test",
        user_id="user_test",
        task_id=task_id,
        thread_id="thread_phase9",
        lease_token="lease",
        cancellation_token=Token(),
        deadline=utcnow() + timedelta(minutes=1),
    )
    with bind_runtime_effect_context(execution, "node_model_phase9"):
        assert model.invoke_text("hello") == "budgeted response"
    with bind_runtime_effect_context(execution, "node_model_phase9"):
        assert model.invoke_text("hello") == "budgeted response"

    assert provider.calls == 1
    with session_factory() as session:
        task = session.get(ResearchTaskRow, task_id)
        assert task is not None
        assert task.task_budget["usage"]["model_tokens"] == 10
        assert Decimal(str(task.task_budget["usage"]["cost"])) == Decimal("0.000013")

    class CancelledToken:
        def is_cancelled(self) -> bool:
            return True

    cancelled = ExecutionContext(
        **{
            **execution.__dict__,
            "cancellation_token": CancelledToken(),
        }
    )
    with bind_runtime_effect_context(cancelled, "node_model_phase9"):
        with pytest.raises(ModelCallError) as error:
            model.invoke_text("cancelled")
    assert error.value.error_code == "cancelled"
    assert provider.calls == 1

    with session_factory() as session, session.begin():
        task = session.get(ResearchTaskRow, task_id)
        assert task is not None
        payload = dict(task.task_budget)
        payload["budget"] = {
            **payload["budget"],
            "max_cost": str(payload["usage"]["cost"]),
        }
        task.task_budget = payload
    exhausted_model = runtime.build_chat_model_for(
        "tenant_test", bindings.get("reasoning"), "phase9_cost_exhausted"
    )
    with bind_runtime_effect_context(execution, "node_model_phase9"):
        with pytest.raises(ModelCallError) as exhausted:
            exhausted_model.invoke_text("new call")
    assert exhausted.value.error_code == "budget_exhausted"
    assert provider.calls == 1


def test_phase9_uses_only_the_three_core_queue_names() -> None:
    class Celery:
        calls: list[tuple[str, str]] = []

        def send_task(self, name: str, *, args=None, queue: str):
            del args
            self.calls.append((name, queue))

    celery = Celery()
    publisher = CeleryJobPublisher(celery)
    publisher.publish_research("task")
    publisher.publish_document("doc")
    publisher.publish_database_write("write", "hash")
    publisher.publish_memory_extraction({"task_id": "task"})
    publisher.publish_memory_embedding({"memory_id": "memory"})
    publisher.publish_memory_gc()
    publisher.publish_knowledge_graph("task")

    assert {queue for _, queue in celery.calls} <= {
        "research",
        "ingestion",
        "export",
    }
    assert ("knowledge_graph.build", "research") in celery.calls
