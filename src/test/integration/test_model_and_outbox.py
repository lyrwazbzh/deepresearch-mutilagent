from __future__ import annotations

import pytest
from langchain_core.language_models.chat_models import BaseChatModel
from sqlalchemy import select

from src.agent.schema.model_config import FrozenModelBinding, ResolvedModelRevision
from src.agent.backends.events import InMemoryEventBroker
from src.agent.llm.runtime import ModelCallError, ModelResolver, ModelRuntime
from src.agent.backends.persistence.models import (
    ModelBindingRow,
    ModelConfigHeadRow,
    ModelConfigRow,
    OutboxMessageRow,
    ResearchEventRow,
)
from src.agent.task_service import CreateTaskRequest
from src.agent.workers.outbox import OutboxDispatcher


class Jobs:
    def __init__(self) -> None:
        self.task_ids: list[str] = []
        self.doc_ids: list[str] = []

    def publish_research(self, task_id: str) -> None:
        self.task_ids.append(task_id)

    def publish_document(self, doc_id: str) -> None:
        self.doc_ids.append(doc_id)


class BrokenBroker(InMemoryEventBroker):
    async def publish(self, tenant_id: str, task_id: str, event_id: int) -> None:
        raise ConnectionError("redis unavailable")


class Provider:
    def __init__(self) -> None:
        self.calls = 0

    def chat(self, config, prompt: str) -> str:
        self.calls += 1
        if config.model_config_id == "model_test":
            return f"managed:{prompt}"
        raise ModelCallError("unavailable")


class FallbackProvider:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def chat(self, config, prompt: str) -> str:
        self.calls.append(config.model_config_id)
        if config.model_config_id == "model_test":
            raise ModelCallError("primary failed")
        return f"fallback:{prompt}"


def test_runtime_returns_langchain_chat_model(session_factory) -> None:
    provider = Provider()
    runtime = ModelRuntime(session_factory, provider_factory=lambda _name: provider)
    binding = FrozenModelBinding(
        binding_key="reasoning",
        purpose="chat",
        primary=ResolvedModelRevision(model_config_id="model_test", version=1),
    )
    model = runtime.build_chat_model("tenant_test", binding)
    assert isinstance(model, BaseChatModel)
    assert model.invoke_text("hello") == "managed:hello"
    assert model.invoke("hello").content == "managed:human: hello"


def test_resolver_freezes_priority_order_and_runtime_uses_only_that_fallback(
    session_factory,
) -> None:
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
        session.flush()
        session.add(
            ModelBindingRow(
                tenant_id="tenant_test",
                binding_key="reasoning",
                purpose="chat",
                priority=1,
                model_config_id="model_fallback",
            )
        )
    resolver = ModelResolver()
    with session_factory() as session:
        frozen = resolver.resolve_snapshot(session, "tenant_test").get("reasoning")
    assert frozen.primary.model_config_id == "model_test"
    assert [item.model_config_id for item in frozen.fallback] == ["model_fallback"]

    provider = FallbackProvider()
    runtime = ModelRuntime(session_factory, provider_factory=lambda _name: provider)
    assert runtime.build_chat_model("tenant_test", frozen).invoke_text("hello") == "fallback:hello"
    assert provider.calls == ["model_test", "model_test", "model_fallback"]


def test_task_freezes_all_phase1_bindings_and_worker_does_not_resolve_again(
    make_container, session_factory
) -> None:
    container, _ = make_container(
        {"routing": [{"mode": "direct"}], "reasoning": ["answer"]}
    )
    created = container.task_service.create(
        "tenant_test", "user_test", "freeze-key", CreateTaskRequest(query="q")
    )
    assert container.model_resolver.resolve_calls == 1
    container.worker.run(created.projection.task_id)
    assert container.model_resolver.resolve_calls == 1


def test_outbox_dispatches_schedule_and_live_event_at_least_once(
    make_container, session_factory
) -> None:
    container, _ = make_container(
        {"routing": [{"mode": "direct"}], "reasoning": ["answer"]}
    )
    created = container.task_service.create(
        "tenant_test", "user_test", "outbox-key", CreateTaskRequest(query="q")
    )
    container.worker.run(created.projection.task_id)
    jobs = Jobs()
    broker = InMemoryEventBroker()
    dispatcher = OutboxDispatcher(session_factory, jobs, broker)
    assert dispatcher.dispatch_once() >= 4
    assert jobs.task_ids == [created.projection.task_id]
    assert dispatcher.dispatch_once() == 0


def test_redis_failure_does_not_remove_committed_postgres_event(
    make_container, session_factory
) -> None:
    container, _ = make_container(
        {"routing": [{"mode": "direct"}], "reasoning": ["answer"]}
    )
    created = container.task_service.create(
        "tenant_test", "user_test", "broken-redis", CreateTaskRequest(query="q")
    )
    container.worker.run(created.projection.task_id)
    dispatcher = OutboxDispatcher(session_factory, Jobs(), BrokenBroker())
    with pytest.raises(ConnectionError, match="redis unavailable"):
        dispatcher.dispatch_once()
    with session_factory() as session:
        assert session.scalar(select(ResearchEventRow).limit(1)) is not None
        event_outbox = session.scalars(
            select(OutboxMessageRow).where(OutboxMessageRow.kind == "research.event.publish")
        ).all()
        assert event_outbox and all(row.dispatched_at is None for row in event_outbox)


def test_outbox_dispatches_document_ingestion_job(make_container, session_factory) -> None:
    container, _ = make_container({})
    doc_id = container.document_service.create(
        tenant_id="tenant_test",
        name="queued.txt",
        media_type="text/plain",
        data=b"queued document",
    )
    jobs = Jobs()
    dispatcher = OutboxDispatcher(session_factory, jobs, InMemoryEventBroker())
    assert dispatcher.dispatch_once() == 1
    assert jobs.doc_ids == [doc_id]
    assert dispatcher.dispatch_once() == 0
