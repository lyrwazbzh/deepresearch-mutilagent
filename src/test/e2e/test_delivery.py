from __future__ import annotations

from dataclasses import dataclass

import pytest
from sqlalchemy import select
from fastapi.testclient import TestClient

from src.agent.config import Settings
from src.api_view.web_main import create_app
from src.agent.container import build_container
from src.agent.tools.database_write import CompiledWrite, PlanHashMismatchError
from src.agent.identity import content_hash
from src.agent.schema.state import (
    CoverageResult,
    CriticResult,
    Evidence,
    HypothesisPlan,
    ResearchState,
    RouteDecision,
)
from src.agent.schema.write import WriteResult
from src.agent.backends.events import InMemoryEventBroker
from src.agent.llm.runtime import FakeModelRuntime
from src.agent.backends.persistence.models import (
    AuditLogRow,
    DataSourceRow,
    DataSourceTablePolicyRow,
    DataSourceUserGrantRow,
    NodeRunRow,
    PendingWriteRow,
    ResearchCheckpointRow,
    ResearchTaskRow,
)
from src.agent.task_service import CreateTaskRequest


def settings() -> Settings:
    return Settings(
        database_url="sqlite://",
        redis_url="redis://unused",
        celery_broker_url="redis://unused",
        default_tenant_id="tenant_test",
        default_user_id="user_test",
        model_mode="fake",
        phase2_enabled=True,
        phase3_enabled=True,
        phase4_enabled=True,
    )


def report(evidence_id: str, *, cited: bool = True) -> dict:
    return {
        "report_version": 1,
        "title": "Approved research",
        "sections": [
            {
                "section_id": "summary",
                "heading": "Summary",
                "level": 2,
                "blocks": [
                    {
                        "type": "paragraph",
                        "segments": [
                            {
                                "type": "claim",
                                "claim_id": "claim_1",
                                "text": "The approved evidence supports the result.",
                                "citation_ids": ["cite_1"] if cited else [],
                            }
                        ],
                    }
                ],
            }
        ],
        "citations": (
            [{"citation_id": "cite_1", "evidence_id": evidence_id}] if cited else []
        ),
    }


def approved_state(task: ResearchTaskRow, *, write: bool) -> ResearchState:
    data = {"rows": [["approved"]]}
    evidence = Evidence(
        evidence_id="ev_approved",
        hypothesis_id="h1",
        summary="Approved source",
        excerpt="approved",
        source_type="sql",
        stance="support",
        relevance=1,
        data=data,
        content_hash=content_hash(data),
    )
    payload = ResearchState.model_validate(task.latest_state)
    payload.route = RouteDecision(mode="deep")
    payload.plan = HypothesisPlan.model_validate(
        {
            "hypotheses": [
                {
                    "hypothesis_id": "h1",
                    "claim": "claim",
                    "evidence_needed": ["source"],
                    "refuted_if": ["counter"],
                    "priority": 1,
                    "queries": [{"query_id": "q1", "channel": "sql", "text": "q"}],
                }
            ],
            "write_intent": (
                {
                    "intent_id": "intent_1",
                    "description": "Mark account active",
                    "datasource_id": "ds1",
                }
                if write
                else None
            ),
        }
    )
    payload.evidence_pool = [evidence]
    payload.coverage = CoverageResult(complete=True)
    payload.critic = CriticResult(score=8, decision="pass", gaps=[])
    return payload


def create_at_critic(container, session_factory, key: str, *, write: bool) -> str:
    task_id = container.task_service.create(
        "tenant_test", "user_test", key, CreateTaskRequest(query="deliver research")
    ).projection.task_id
    with session_factory() as session, session.begin():
        task = session.get(ResearchTaskRow, task_id)
        state = approved_state(task, write=write)
        task.latest_state = state.model_dump(mode="json")
        checkpoint = session.scalar(
            select(ResearchCheckpointRow).where(ResearchCheckpointRow.task_id == task_id)
        )
        checkpoint.state_snapshot = task.latest_state
        checkpoint.graph_position = "critic"
    return task_id


def seed_write_policy(session_factory) -> None:
    with session_factory() as session, session.begin():
        session.add(
            DataSourceRow(
                datasource_id="ds1",
                tenant_id="tenant_test",
                name="Business DB",
                host="localhost",
                port=5432,
                database_name="business",
                ssl_mode="require",
                credential_ref="PHASE4_TEST_DB_SECRET",
                enabled=True,
            )
        )
        session.add(
            DataSourceUserGrantRow(
                tenant_id="tenant_test",
                datasource_id="ds1",
                user_id="user_test",
                access_level="write",
            )
        )
        session.add(
            DataSourceTablePolicyRow(
                tenant_id="tenant_test",
                datasource_id="ds1",
                schema_name="public",
                table_name="accounts",
                readable_columns=["id", "status"],
                writable_columns=["id", "status"],
                allow_insert=True,
                allow_update=True,
                allow_delete=False,
                max_write_rows=1,
            )
        )


def write_plan() -> dict:
    return {
        "plan_id": "plan_1",
        "datasource_id": "ds1",
        "operation": "update",
        "table": "public.accounts",
        "values": {"status": "active"},
        "filters": [{"column": "id", "op": "=", "value": 7}],
        "reason": "Apply the approved correction",
    }


def test_no_write_finalizes_report_and_completes_task(session_factory) -> None:
    runtime = FakeModelRuntime({"writing:lead_writer": [report("ev_approved")]})
    container = build_container(
        settings(), session_factory=session_factory, model_runtime=runtime,
        event_broker=InMemoryEventBroker(),
    )
    task_id = create_at_critic(container, session_factory, "phase4-no-write", write=False)
    result = container.worker.run(task_id)
    assert result.outcome == "success"
    assert result.state.report.final == result.state.report.draft
    assert container.workspace_service.read(
        "tenant_test", task_id, "reports/versions/1/report.json"
    )
    assert container.workspace_service.read(
        "tenant_test", task_id, "reports/versions/1/report.md"
    )
    with session_factory() as session:
        assert session.get(ResearchTaskRow, task_id).status == "completed"
        node_names = session.scalars(
            select(NodeRunRow.node_name).where(NodeRunRow.task_id == task_id)
        ).all()
        assert "graph_success" in node_names
    with session_factory() as session, session.begin():
        task_row = session.get(ResearchTaskRow, task_id)
        task_row.status = "pending"
        task_row.lease_token = None
        task_row.leased_until = None
        task_row.budget_accounted_at = None
    replay = container.worker.run(task_id)
    assert replay.outcome == "success"
    assert replay.state.report.final.version == 1


def test_invalid_report_revises_once_before_finalize(session_factory) -> None:
    runtime = FakeModelRuntime(
        {
            "writing:lead_writer": [report("ev_approved", cited=False)],
            "writing:revise": [report("ev_approved")],
        }
    )
    container = build_container(
        settings(), session_factory=session_factory, model_runtime=runtime,
        event_broker=InMemoryEventBroker(),
    )
    task_id = create_at_critic(container, session_factory, "phase4-revise", write=False)
    result = container.worker.run(task_id)
    assert result.outcome == "success"
    assert result.state.execution.revision_round == 1
    assert result.state.report.final is not None


def test_report_revision_exhaustion_fails_without_draft(session_factory) -> None:
    invalid = report("ev_approved", cited=False)
    runtime = FakeModelRuntime(
        {
            "writing:lead_writer": [invalid],
            "writing:revise": [invalid, invalid],
        }
    )
    container = build_container(
        settings(), session_factory=session_factory, model_runtime=runtime,
        event_broker=InMemoryEventBroker(),
    )
    task_id = create_at_critic(container, session_factory, "phase4-exhaust", write=False)
    result = container.worker.run(task_id)
    assert result.outcome == "failure"
    assert result.failure_reason == "report_validation_failed"
    assert result.state.report.draft is None
    with session_factory() as session:
        assert session.get(ResearchTaskRow, task_id).status == "failed"


def test_write_reject_resumes_report_without_dml(session_factory) -> None:
    seed_write_policy(session_factory)
    runtime = FakeModelRuntime(
        {
            "data:prepare_write": [write_plan()],
            "writing:lead_writer": [report("ev_approved")],
        }
    )
    container = build_container(
        settings(), session_factory=session_factory, model_runtime=runtime,
        event_broker=InMemoryEventBroker(),
    )
    task_id = create_at_critic(container, session_factory, "phase4-reject", write=True)
    paused = container.worker.run(task_id)
    assert paused.outcome == "paused"
    write_id = paused.state.pending_write
    assert write_id
    client = TestClient(create_app(container))
    assert client.get(
        f"/api/v1/tasks/{task_id}/pending-writes/{write_id}",
        headers={"X-Tenant-ID": "tenant_test", "X-User-ID": "other_user"},
    ).status_code == 404
    assert client.get(
        f"/api/v1/tasks/{task_id}/pending-writes/{write_id}",
        headers={"X-Tenant-ID": "tenant_test", "X-User-ID": "user_test"},
    ).status_code == 200
    rejected = client.post(
        f"/api/v1/tasks/{task_id}/pending-writes/{write_id}/reject",
        json={"reason": "Do not change production data"},
        headers={
            "X-Tenant-ID": "tenant_test",
            "X-User-ID": "user_test",
            "Idempotency-Key": "reject-phase4-write",
        },
    )
    assert rejected.status_code == 202
    assert rejected.json()["status"] == "rejected"
    completed = container.worker.run(task_id)
    assert completed.outcome == "success"
    with session_factory() as session:
        assert session.get(PendingWriteRow, ("tenant_test", write_id)).result_json is None
        assert session.get(ResearchTaskRow, task_id).status == "completed"


@dataclass
class FakeTarget:
    calls: list[CompiledWrite]

    def execute(self, **kwargs):
        self.calls.append(kwargs["compiled"])
        return WriteResult(
            status="executed",
            affected_rows=1,
            operation_key=kwargs["operation_key"],
        )


class FakeTargetFactory:
    def __init__(self) -> None:
        self.target = FakeTarget([])

    def create(self, datasource, credential):
        del datasource, credential
        return self.target


def test_write_approve_executes_once_then_resumes_report(
    session_factory, monkeypatch
) -> None:
    seed_write_policy(session_factory)
    monkeypatch.setenv(
        "PHASE4_TEST_DB_SECRET",
        '{"read":{"username":"reader","password":"x"},'
        '"write":{"username":"writer","password":"y"}}',
    )
    factory = FakeTargetFactory()
    runtime = FakeModelRuntime(
        {
            "data:prepare_write": [write_plan()],
            "writing:lead_writer": [report("ev_approved")],
        }
    )
    container = build_container(
        settings(), session_factory=session_factory, model_runtime=runtime,
        event_broker=InMemoryEventBroker(), write_target_factory=factory,
    )
    task_id = create_at_critic(container, session_factory, "phase4-approve", write=True)
    paused = container.worker.run(task_id)
    write_id = paused.state.pending_write
    assert write_id
    pending = container.pending_write_service.get(
        "tenant_test", "user_test", task_id, write_id
    )
    with pytest.raises(PlanHashMismatchError):
        container.pending_write_service.approve(
            tenant_id="tenant_test", user_id="user_test", task_id=task_id,
            write_id=write_id, plan_hash="0" * 64, comment=None,
        )
    approved = container.pending_write_service.approve(
        tenant_id="tenant_test", user_id="user_test", task_id=task_id,
        write_id=write_id, plan_hash=pending.plan_hash, comment="approved",
    )
    assert approved.status == "approved"
    with session_factory() as session:
        assert session.get(ResearchTaskRow, task_id).status == "paused"
    result = container.write_executor.execute(write_id, pending.plan_hash)
    assert result.status == "executed"
    assert len(factory.target.calls) == 1
    replay = container.write_executor.execute(write_id, pending.plan_hash)
    assert replay.status == "executed"
    assert len(factory.target.calls) == 1
    completed = container.worker.run(task_id)
    assert completed.outcome == "success"
    with session_factory() as session:
        assert session.get(ResearchTaskRow, task_id).status == "completed"
        assert session.scalar(select(AuditLogRow).where(AuditLogRow.action == "pending_write.execute"))


class CommitUnknownTarget:
    def __init__(self) -> None:
        self.calls = 0
        self.committed = False

    def execute(self, **kwargs):
        self.calls += 1
        if not self.committed:
            self.committed = True
            raise ConnectionError("commit acknowledgement was lost")
        return WriteResult(
            status="executed",
            affected_rows=1,
            operation_key=kwargs["operation_key"],
            replayed=True,
        )


class CommitUnknownFactory:
    def __init__(self) -> None:
        self.target = CommitUnknownTarget()

    def create(self, datasource, credential):
        del datasource, credential
        return self.target


def test_commit_unknown_retry_recovers_without_reapplying_dml(
    session_factory, monkeypatch
) -> None:
    seed_write_policy(session_factory)
    monkeypatch.setenv(
        "PHASE4_TEST_DB_SECRET",
        '{"read":{"username":"reader","password":"x"},'
        '"write":{"username":"writer","password":"y"}}',
    )
    factory = CommitUnknownFactory()
    runtime = FakeModelRuntime({"data:prepare_write": [write_plan()]})
    container = build_container(
        settings(),
        session_factory=session_factory,
        model_runtime=runtime,
        event_broker=InMemoryEventBroker(),
        write_target_factory=factory,
    )
    task_id = create_at_critic(container, session_factory, "phase4-commit-unknown", write=True)
    paused = container.worker.run(task_id)
    pending = container.pending_write_service.get(
        "tenant_test", "user_test", task_id, paused.state.pending_write
    )
    container.pending_write_service.approve(
        tenant_id="tenant_test",
        user_id="user_test",
        task_id=task_id,
        write_id=pending.write_id,
        plan_hash=pending.plan_hash,
        comment=None,
    )

    with pytest.raises(ConnectionError, match="acknowledgement"):
        container.write_executor.execute(pending.write_id, pending.plan_hash)
    with session_factory() as session:
        executing = session.get(PendingWriteRow, ("tenant_test", pending.write_id))
        assert executing.status == "executing"
        assert executing.result_json is None

    recovered = container.write_executor.execute(pending.write_id, pending.plan_hash)
    replay = container.write_executor.execute(pending.write_id, pending.plan_hash)
    assert recovered.status == replay.status == "executed"
    assert recovered.replayed is True
    assert factory.target.calls == 2


def test_policy_change_marks_write_stale_and_requires_new_approval(
    session_factory, monkeypatch
) -> None:
    seed_write_policy(session_factory)
    monkeypatch.setenv(
        "PHASE4_TEST_DB_SECRET",
        '{"read":{"username":"reader","password":"x"},'
        '"write":{"username":"writer","password":"y"}}',
    )
    factory = FakeTargetFactory()
    runtime = FakeModelRuntime({"data:prepare_write": [write_plan(), write_plan()]})
    container = build_container(
        settings(), session_factory=session_factory, model_runtime=runtime,
        event_broker=InMemoryEventBroker(), write_target_factory=factory,
    )
    task_id = create_at_critic(container, session_factory, "phase4-stale", write=True)
    first_pause = container.worker.run(task_id)
    old_write_id = first_pause.state.pending_write
    assert old_write_id
    old = container.pending_write_service.get(
        "tenant_test", "user_test", task_id, old_write_id
    )
    container.pending_write_service.approve(
        tenant_id="tenant_test", user_id="user_test", task_id=task_id,
        write_id=old_write_id, plan_hash=old.plan_hash, comment=None,
    )
    with session_factory() as session, session.begin():
        policy = session.get(
            DataSourceTablePolicyRow,
            ("tenant_test", "ds1", "public", "accounts"),
        )
        policy.max_write_rows = 2
    stale = container.write_executor.execute(old_write_id, old.plan_hash)
    assert stale.status == "stale"
    assert not factory.target.calls
    second_pause = container.worker.run(task_id)
    assert second_pause.outcome == "paused"
    assert second_pause.state.pending_write != old_write_id
    with session_factory() as session:
        assert session.get(PendingWriteRow, ("tenant_test", old_write_id)).status == "stale"
