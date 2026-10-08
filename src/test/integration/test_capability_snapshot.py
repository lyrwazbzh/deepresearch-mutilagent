from __future__ import annotations

from sqlalchemy import select

from src.agent.schema.capability import CapabilitySnapshot
from src.agent.schema.state import RouteDecision
from src.agent.llm.runtime import ModelResolver, ModelRuntime, PHASE1_BINDINGS
from src.agent.backends.persistence.models import ResearchTaskRow
from src.agent.task_service import CreateTaskRequest


def test_task_creation_freezes_phase7_capability_contract(make_container, session_factory) -> None:
    container, _ = make_container({})
    created = container.task_service.create(
        "tenant_test",
        "user_test",
        "phase7-snapshot",
        CreateTaskRequest(query="research the market", options={"datasource_ids": ["ds1"]}),
    )
    with session_factory() as session:
        row = session.scalar(
            select(ResearchTaskRow).where(
                ResearchTaskRow.task_id == created.projection.task_id
            )
        )
        assert row is not None
        snapshot = CapabilitySnapshot.model_validate(row.capability_snapshot)

    assert snapshot.datasource_ids == ["ds1"]
    assert len(snapshot.policy_revision) == 64
    assert all(len(item.content_hash) == 64 for item in snapshot.skills)
    assert all(len(item.schema_hash) == 64 and item.provider_ref for item in snapshot.tools)
    assert "knowledge-graph" not in {item.name for item in snapshot.skills}
    assert not {"build_knowledge_graph", "query_knowledge_graph"} & {
        item.name for item in snapshot.tools
    }
    assert {"data-analysis", "visualization", "rendered-web"} <= {
        item.name for item in snapshot.skills
    }


def test_context_packets_expose_only_agent_l1_skill_metadata(make_container, session_factory) -> None:
    container, _ = make_container({})
    created = container.task_service.create(
        "tenant_test",
        "user_test",
        "phase7-context",
        CreateTaskRequest(query="analyze data"),
    )
    with session_factory() as session:
        row = session.get(ResearchTaskRow, created.projection.task_id)
        assert row is not None
        snapshot = CapabilitySnapshot.model_validate(row.capability_snapshot)

    from datetime import timedelta

    from src.agent.schema.task import TaskBudget
    from src.agent.schema.runtime import ExecutionContext
    from src.agent.schema.state import ResearchState
    from src.agent.backends.persistence.models import utcnow

    class Token:
        def is_cancelled(self) -> bool:
            return False

    execution = ExecutionContext(
        tenant_id="tenant_test",
        user_id="user_test",
        task_id=created.projection.task_id,
        thread_id=created.projection.thread_id,
        lease_token="lease",
        cancellation_token=Token(),
        deadline=utcnow() + timedelta(minutes=1),
        capability_snapshot=snapshot.model_dump(mode="json"),
    )
    state = ResearchState(
        tenant_id="tenant_test",
        user_id="user_test",
        task_id=created.projection.task_id,
        thread_id=created.projection.thread_id,
        query="analyze data",
    )
    data_packet = container.node_runner.context_builder.build(
        "data_analyst", state, execution, TaskBudget()
    )
    code_packet = container.node_runner.context_builder.build(
        "code_wizard", state, execution, TaskBudget()
    )
    route_packet = container.node_runner.context_builder.build(
        "route", state, execution, TaskBudget()
    )
    assert [item["name"] for item in data_packet.skills] == ["data-analysis"]
    assert [item["name"] for item in code_packet.skills] == ["visualization"]
    assert route_packet.skills == []
    assert "instructions" not in data_packet.skills[0]
    assert "latest_state" not in data_packet.model_dump_json()


def test_runtime_retries_invalid_structured_output_before_accepting_state_value(
    session_factory,
) -> None:
    class Provider:
        def __init__(self) -> None:
            self.responses = iter(
                [
                    '{"mode":"focused"}',
                    '{"mode":"direct","target_agent":null}',
                ]
            )
            self.calls = 0

        def chat(self, config, prompt):
            del config, prompt
            self.calls += 1
            return next(self.responses)

        def embed(self, config, texts):
            raise AssertionError((config, texts))

        def rerank(self, config, query, candidates):
            raise AssertionError((config, query, candidates))

    provider = Provider()
    with session_factory() as session:
        snapshot = ModelResolver().resolve_snapshot(
            session, "tenant_test", PHASE1_BINDINGS
        )
    model = ModelRuntime(
        session_factory, provider_factory=lambda _provider: provider
    ).build_chat_model("tenant_test", snapshot.get("routing"))
    decision = model.invoke_structured("Return RouteDecision JSON", RouteDecision)
    assert decision.mode == "direct"
    assert provider.calls == 2
