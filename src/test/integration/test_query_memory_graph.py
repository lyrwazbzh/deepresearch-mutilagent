from __future__ import annotations

from collections import deque

from sqlalchemy import select

from src.agent.config import Settings
from src.agent.container import build_container
from src.agent.backends.events import InMemoryEventBroker
from src.agent.llm.runtime import FakeModelRuntime
from src.agent.backends.persistence.models import (
    ConversationMessageRow,
    NodeRunRow,
    OutboxMessageRow,
    ResearchMemoryRow,
    ResearchTaskRow,
)
from src.agent.task_service import CreateTaskRequest, ReplanRequest, new_id


PLAN = {
    "hypotheses": [
        {
            "hypothesis_id": "h1",
            "claim": "A claim",
            "evidence_needed": ["primary evidence"],
            "refuted_if": ["contradiction"],
            "priority": 1,
            "queries": [{"query_id": "q1", "channel": "web", "text": "source"}],
        }
    ],
    "deepen_history": [],
}


def settings(*, phase5: bool) -> Settings:
    return Settings(
        database_url="sqlite://",
        redis_url="redis://unused",
        celery_broker_url="redis://unused",
        default_tenant_id="tenant_test",
        default_user_id="user_test",
        model_mode="fake",
        phase2_enabled=False,
        phase5_enabled=phase5,
        query_completion_message_limit=3,
        query_completion_token_budget=100,
    )


def test_query_completion_is_thread_scoped_bounded_and_replan_replaces_query(
    session_factory,
) -> None:
    container = build_container(
        settings(phase5=True),
        session_factory=session_factory,
        model_runtime=FakeModelRuntime(),
        event_broker=InMemoryEventBroker(),
    )
    first = container.task_service.create(
        "tenant_test", "user_test", "query-first", CreateTaskRequest(query="first question")
    )
    with session_factory() as session, session.begin():
        session.add(
            ConversationMessageRow(
                message_id=new_id("msg"),
                tenant_id="tenant_test",
                user_id="user_test",
                thread_id=first.projection.thread_id,
                task_id=first.projection.task_id,
                role="assistant",
                content="first answer",
            )
        )
    second = container.task_service.create(
        "tenant_test",
        "user_test",
        "query-second",
        CreateTaskRequest(query="compare the second item", thread_id=first.projection.thread_id),
    )
    with session_factory() as session:
        second_row = session.get(ResearchTaskRow, second.projection.task_id)
        assert second_row is not None
        query = second_row.latest_state["query"]
        assert "first question" in query
        assert "first answer" in query
        assert query.endswith("compare the second item")

    other = container.task_service.create(
        "tenant_test", "user_test", "query-other", CreateTaskRequest(query="secret other")
    )
    with session_factory() as session, session.begin():
        row = session.get(ResearchTaskRow, second.projection.task_id)
        assert row is not None
        row.status = "running"
        row.lease_token = "old-lease"
    replanned = container.task_service.replan(
        "tenant_test",
        "user_test",
        second.projection.task_id,
        "replan-key",
        ReplanRequest(instruction="now focus on primary sources"),
    )
    assert replanned.task_status.value == "pending"
    with session_factory() as session:
        row = session.get(ResearchTaskRow, second.projection.task_id)
        assert row is not None
        assert row.latest_state["query"].endswith("now focus on primary sources")
        assert "secret other" not in row.latest_state["query"]
        assert row.latest_state["memory_context"] is None
        assert row.lease_token is None
        messages = list(
            session.scalars(
                select(ConversationMessageRow).where(
                    ConversationMessageRow.thread_id == other.projection.thread_id
                )
            ).all()
        )
        assert any(message.content == "secret other" for message in messages)


def test_plan_recall_checkpoints_refs_and_context_builder_rehydrates_only_for_planning(
    session_factory,
) -> None:
    runtime = FakeModelRuntime({"routing": [{"mode": "deep"}], "reasoning": [PLAN]})
    container = build_container(
        settings(phase5=True),
        session_factory=session_factory,
        model_runtime=runtime,
        event_broker=InMemoryEventBroker(),
    )
    created = container.task_service.create(
        "tenant_test",
        "user_test",
        "phase5-plan",
        CreateTaskRequest(query="[deep] research this market"),
    )
    assert created.projection.message_id is not None
    memory = container.memory_service.remember(
        tenant_id="tenant_test",
        user_id="user_test",
        topic="output.language.default",
        content="Use concise English output.",
        supporting_message_ids=[created.projection.message_id],
        logical_key="plan-memory",
    ).memory

    result = container.worker.run(created.projection.task_id)
    assert result.outcome == "phase1_deep_planned"
    assert result.state.memory_context is not None
    assert result.state.memory_context.items[0].memory_id == memory.memory_id
    state_payload = result.state.model_dump(mode="json")
    assert "Use concise English output." not in str(state_payload["memory_context"])
    assert "Use concise English output." in runtime.calls["reasoning"][0]
    with session_factory() as session:
        nodes = {
            row.node_name
            for row in session.scalars(
                select(NodeRunRow).where(NodeRunRow.task_id == created.projection.task_id)
            ).all()
        }
    assert {"route", "memory_recall_plan", "chief_plan"} <= nodes


def test_completed_visible_turn_requests_extraction_and_rejects_assistant_only_fact(
    session_factory,
) -> None:
    runtime = FakeModelRuntime(
        {
            "routing": [{"mode": "direct"}],
            "reasoning": ["final answer"],
        }
    )
    container = build_container(
        settings(phase5=True),
        session_factory=session_factory,
        model_runtime=runtime,
        event_broker=InMemoryEventBroker(),
    )
    created = container.task_service.create(
        "tenant_test",
        "user_test",
        "phase5-extraction",
        CreateTaskRequest(query="Please remember that I prefer primary papers."),
    )
    assert created.projection.message_id is not None
    container.worker.run(created.projection.task_id)
    with session_factory() as session:
        extraction = session.scalar(
            select(OutboxMessageRow).where(
                OutboxMessageRow.kind == "memory.extraction.requested"
            )
        )
        assistant = session.scalar(
            select(ConversationMessageRow).where(
                ConversationMessageRow.task_id == created.projection.task_id,
                ConversationMessageRow.role == "assistant",
            )
        )
        assert extraction is not None and assistant is not None
        payload = dict(extraction.payload_json)
    extracted_batch = {
        "candidates": [
            {
                "topic": "source preference",
                "category": "research_preference",
                "content": "Prefer primary papers.",
                "confidence": 0.95,
                "supporting_user_message_ids": [created.projection.message_id],
            },
            {
                "topic": "output language",
                "category": "output_preference",
                "content": "Use French.",
                "confidence": 0.95,
                "supporting_user_message_ids": [assistant.message_id],
            },
        ]
    }
    runtime._scripted["reasoning:memory_extractor"] = deque(
        [extracted_batch, extracted_batch]
    )
    assert container.memory_extraction_worker.run(payload) == 1
    assert container.memory_extraction_worker.run(payload) == 1
    with session_factory() as session:
        memories = list(session.scalars(select(ResearchMemoryRow)).all())
        assert len(memories) == 1
        assert memories[0].topic == "research.source.preference"
