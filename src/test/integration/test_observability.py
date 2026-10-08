from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import httpx
import pytest
from sqlalchemy import select

from src.agent.backends.audit import AuditService
from src.agent.tools.web import SearchHit, SearchWebAdapter, SearchWebArgs
from src.agent.schema.model_config import FrozenModelBinding, ResolvedModelRevision
from src.agent.schema.task import TaskBudget
from src.agent.schema.runtime import ExecutionContext, NodeResult, StatePatch
from src.agent.schema.state import ResearchState
from src.agent.main_agent import GraphRunResult
from src.agent.llm.runtime import ModelRuntime, ProviderChatResponse
from src.agent.backends.observability import CostQueryService, PostgresSpanExporter, Telemetry, TraceScope
from src.agent.backends.persistence.models import (
    AuditLogRow,
    ModelConfigHeadRow,
    ModelConfigRow,
    ModelPricingRow,
    ObservabilitySpanRow,
    ResearchCheckpointRow,
    ResearchTaskRow,
    utcnow,
)
from src.agent.runtime.tool_runtime import ToolDefinition, ToolPolicy, ToolRegistry, ToolRuntime
from src.agent.errors import TaskPauseRequested
from src.agent.task_service import CreateTaskRequest
from src.agent.workers.research import DatabaseCancellationToken, ResearchWorker
from src.test.harness import ProviderAttemptSequence, in_memory_telemetry


def _fallback_binding(session_factory) -> FrozenModelBinding:
    with session_factory() as session, session.begin():
        session.add(
            ModelConfigRow(
                model_config_id="model_fallback",
                version=1,
                tenant_id="tenant_test",
                user_id="user_test",
                provider="fake",
                model="fallback",
                purpose="chat",
            )
        )
        session.add(
            ModelConfigHeadRow(
                tenant_id="tenant_test",
                user_id="user_test",
                model_config_id="model_fallback",
                current_version=1,
                enabled_for_new_tasks=True,
            )
        )
        effective = datetime(2020, 1, 1, tzinfo=timezone.utc)
        for model in ("test", "fallback"):
            session.add(
                ModelPricingRow(
                    tenant_id="tenant_test",
                    provider="fake",
                    model=model,
                    currency="USD",
                    effective_at=effective,
                    input_price_per_1m_tokens=Decimal("1"),
                    output_price_per_1m_tokens=Decimal("2"),
                )
            )
    return FrozenModelBinding(
        binding_key="reasoning",
        purpose="chat",
        primary=ResolvedModelRevision(model_config_id="model_test", version=1),
        fallback=[ResolvedModelRevision(model_config_id="model_fallback", version=1)],
    )


def test_model_attempts_client_spans_usage_cost_and_sensitive_data(session_factory) -> None:
    telemetry, exporter = in_memory_telemetry(session_factory)
    first = httpx.ReadTimeout("provider timeout")
    first.input_tokens = 10
    second = httpx.ReadTimeout("provider timeout")
    provider = ProviderAttemptSequence(
        chat=[
            first,
            second,
            ProviderChatResponse(content="safe answer", input_tokens=100, output_tokens=50),
        ]
    )
    runtime = ModelRuntime(
        session_factory,
        provider_factory=lambda _provider: provider,
        telemetry=telemetry,
    )
    model = runtime.build_chat_model("tenant_test", _fallback_binding(session_factory))

    assert model.invoke_text("SECRET PROMPT BODY") == "safe answer"
    spans = exporter.get_finished_spans()
    attempts = [span for span in spans if span.name == "gen_ai.chat"]
    assert [span.attributes["research.model_attempt_no"] for span in attempts] == [1, 2, 3]
    assert [span.attributes["research.model_attempt_kind"] for span in attempts] == [
        "primary",
        "retry",
        "fallback",
    ]
    assert attempts[0].attributes["error.category"] == "provider"
    assert attempts[0].attributes["gen_ai.usage.input_tokens"] == 10
    assert Decimal(attempts[0].attributes["app.cost.amount"]) == Decimal("0.00001")
    assert attempts[-1].attributes["gen_ai.usage.input_tokens"] == 100
    assert attempts[-1].attributes["gen_ai.usage.output_tokens"] == 50
    assert Decimal(attempts[-1].attributes["app.cost.amount"]) == Decimal("0.0002")
    assert attempts[-1].attributes["app.cost.currency"] == "USD"
    assert any(span.kind.name == "CLIENT" for span in spans)
    assert "SECRET PROMPT BODY" not in repr([span.attributes for span in spans])


def test_pg_exporter_is_bounded_and_failure_is_best_effort(session_factory) -> None:
    telemetry = Telemetry(session_factory, synchronous=True)
    with telemetry.span(
        "research.run",
        scope=TraceScope("tenant_test", "user_test", "task_test"),
        attributes={"query": "must not persist", "research.depth": 1},
    ):
        pass
    for currency, amount in (("USD", "1.25"), ("CNY", "2.50")):
        with telemetry.span(
            "gen_ai.chat",
            scope=TraceScope("tenant_test", "user_test", "task_test"),
            attributes={
                "gen_ai.provider.name": "fake",
                "gen_ai.request.model": "priced-test-model",
                "gen_ai.operation.name": "chat",
                "app.cost.currency": currency,
                "app.cost.amount": amount,
            },
        ):
            pass
    with session_factory() as session:
        row = session.scalar(
            select(ObservabilitySpanRow).where(
                ObservabilitySpanRow.name == "research.run"
            )
        )
        assert row is not None
        assert (row.tenant_id, row.user_id, row.task_id) == (
            "tenant_test",
            "user_test",
            "task_test",
        )
        assert row.attributes["research.depth"] == 1
        assert "query" not in row.attributes
    assert CostQueryService(session_factory).aggregate(
        "tenant_test", dimensions=("currency",), task_id="task_test"
    ) == [
        {"currency": "CNY", "amount": "2.50"},
        {"currency": "USD", "amount": "1.25"},
    ]

    class BrokenSessionFactory:
        def __call__(self):
            raise ConnectionError("postgres unavailable")

    failed = Telemetry(BrokenSessionFactory(), synchronous=True)  # type: ignore[arg-type]
    with failed.span("research.run", scope=TraceScope("tenant_test")):
        committed_business_value = 7
    assert committed_business_value == 7
    assert failed.metrics.snapshot()["telemetry_export_failed_total{}"] == 1
    assert isinstance(failed.exporter, PostgresSpanExporter)


def test_tool_fallback_events_client_spans_and_denial_audit(session_factory) -> None:
    telemetry, exporter = in_memory_telemetry(session_factory)

    class FailedProvider:
        name = "failed"

        def search(self, query: str, **options):
            del query, options
            raise httpx.ReadTimeout("timeout")

    class GoodProvider:
        name = "good"

        def search(self, query: str, **options):
            del query, options
            return [SearchHit(title="title", url="https://example.com", snippet="s", source="web")]

    registry = ToolRegistry(
        [
            ToolDefinition(
                name="search_web",
                args_schema=SearchWebArgs,
                adapter=SearchWebAdapter([FailedProvider(), GoodProvider()], []),
                provider_ref="web:chain:v1",
            )
        ]
    )
    runtime = ToolRuntime(
        registry,
        telemetry=telemetry,
        audit_service=AuditService(session_factory),
    )
    execution = ExecutionContext(
        tenant_id="tenant_test",
        user_id="user_test",
        task_id="task_test",
        thread_id="thread_test",
        lease_token="lease",
        cancellation_token=type("Token", (), {"is_cancelled": lambda self: False})(),
        deadline=utcnow() + timedelta(minutes=1),
    )
    result = runtime.execute(
        agent_name="deep_scout",
        tool_name="search_web",
        arguments={"query": "private query"},
        execution=execution,
        node_run_id="node_1",
        policy=ToolPolicy(frozenset({"search_web"})),
    )
    assert result.ok
    tool_span = next(span for span in exporter.get_finished_spans() if span.name == "tool.execute")
    assert [event.name for event in tool_span.events] == ["tool.retry", "tool.fallback"]
    assert sum(span.kind.name == "CLIENT" for span in exporter.get_finished_spans()) >= 2
    assert "private query" not in repr(tool_span.attributes)

    denied = runtime.execute(
        agent_name="deep_scout",
        tool_name="search_web",
        arguments={"query": "q"},
        execution=execution,
        node_run_id="node_2",
        policy=ToolPolicy(frozenset()),
    )
    assert denied.error and denied.error.type == "policy_denied"
    with session_factory() as session:
        audit = session.scalar(select(AuditLogRow))
        assert audit is not None
        assert audit.actor_ref == "user:user_test"
        assert audit.action == "tool.policy.denied"
        assert audit.result == "denied"


def _running_node_context(container, session_factory, key: str):
    created = container.task_service.create(
        "tenant_test", "user_test", key, CreateTaskRequest(query="question")
    )
    task, token = container.worker.claim(created.projection.task_id)
    with session_factory() as session:
        checkpoint = session.scalar(
            select(ResearchCheckpointRow).where(
                ResearchCheckpointRow.task_id == task.task_id
            )
        )
    assert checkpoint is not None
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


def test_node_retry_reuses_node_identity_but_emits_attempt_spans(
    make_container, session_factory
) -> None:
    container, _ = make_container({})
    telemetry, exporter = in_memory_telemetry(session_factory)
    container.node_runner.telemetry = telemetry
    task, execution, checkpoint_id = _running_node_context(
        container, session_factory, "phase12-node"
    )

    def handler(_packet, attempt):
        if attempt == 1:
            raise RuntimeError("retry")
        return NodeResult(patch=StatePatch(values={"route": {"mode": "direct"}}))

    kwargs = dict(
        node_name="route",
        node_key="route:phase12",
        context_role="route",
        input_checkpoint_id=checkpoint_id,
        execution=execution,
        state=ResearchState.model_validate(task.latest_state),
        budget=TaskBudget.model_validate(task.task_budget),
        handler=handler,
    )
    with pytest.raises(RuntimeError, match="retry"):
        container.node_runner.execute(**kwargs)
    container.node_runner.execute(**kwargs)
    spans = [span for span in exporter.get_finished_spans() if span.name == "graph.node"]
    assert [span.attributes["research.node_attempt_no"] for span in spans] == [1, 2]
    assert len({span.attributes["research.node_run_id"] for span in spans}) == 1


def test_pause_resume_creates_new_research_run_trace(make_container, session_factory) -> None:
    container, _ = make_container({})
    telemetry, exporter = in_memory_telemetry(session_factory)
    created = container.task_service.create(
        "tenant_test", "user_test", "phase12-runs", CreateTaskRequest(query="q")
    )

    class Graph:
        calls = 0

        def run(self, *, state, execution, **kwargs):
            del kwargs
            self.calls += 1
            if self.calls == 1:
                with session_factory() as session, session.begin():
                    task = session.get(ResearchTaskRow, execution.task_id)
                    task.status = "pausing"
                raise TaskPauseRequested()
            return GraphRunResult(
                outcome="success",
                state=state,
                checkpoint_id="checkpoint_final",
            )

    worker = ResearchWorker(session_factory, Graph(), telemetry=telemetry)
    assert worker.run(created.projection.task_id).outcome == "paused"
    container.task_service.command(
        "tenant_test", "user_test", created.projection.task_id, "resume", "resume-1"
    )
    assert worker.run(created.projection.task_id).outcome == "success"
    spans = [span for span in exporter.get_finished_spans() if span.name == "research.run"]
    assert len(spans) == 2
    assert len({span.context.trace_id for span in spans}) == 2
