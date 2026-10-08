from __future__ import annotations

from sqlalchemy import func, select
from fastapi.testclient import TestClient

from src.api_view.web_main import create_app
from src.agent.config import Settings
from src.agent.container import build_container
from src.agent.memory.domain import MemoryCandidate
from src.agent.backends.events import InMemoryEventBroker
from src.agent.llm.runtime import FakeModelRuntime
from src.agent.backends.persistence.models import OutboxMessageRow, ResearchMemoryRow
from src.agent.errors import IdempotencyConflictError, StaleConflictError
from src.agent.task_service import CreateTaskRequest, new_id
from src.agent.workers.outbox import OutboxDispatcher


def phase5_container(session_factory):
    settings = Settings(
        database_url="sqlite://",
        redis_url="redis://unused",
        celery_broker_url="redis://unused",
        default_tenant_id="tenant_test",
        default_user_id="user_test",
        model_mode="fake",
        phase2_enabled=True,
        phase5_enabled=True,
    )
    return build_container(
        settings,
        session_factory=session_factory,
        model_runtime=FakeModelRuntime(),
        event_broker=InMemoryEventBroker(),
    )


def create_supporting_message(container, key: str = "phase5-memory") -> str:
    created = container.task_service.create(
        "tenant_test",
        "user_test",
        key,
        CreateTaskRequest(query="Remember that I prefer primary papers."),
    )
    assert created.projection.message_id is not None
    return created.projection.message_id


def test_memory_write_dedup_replacement_conflict_resolve_and_forget(session_factory) -> None:
    container = phase5_container(session_factory)
    message_id = create_supporting_message(container)
    service = container.memory_service

    first = service.remember(
        tenant_id="tenant_test",
        user_id="user_test",
        topic="output language",
        content="Use English output.",
        supporting_message_ids=[message_id],
        logical_key="remember-1",
    )
    assert first.disposition == "created"
    assert first.memory.status == "active"
    assert first.memory.confidence == 1.0

    replay = service.remember(
        tenant_id="tenant_test",
        user_id="user_test",
        topic="output language",
        content="Use English output.",
        supporting_message_ids=[message_id],
        logical_key="remember-1",
    )
    assert replay.memory.memory_id == first.memory.memory_id
    assert replay.memory.revision == 1

    duplicate = service.remember(
        tenant_id="tenant_test",
        user_id="user_test",
        topic="output.language.default",
        content="Use English output.",
        supporting_message_ids=[message_id],
        logical_key="remember-2",
    )
    assert duplicate.disposition == "exact_dedup"
    assert duplicate.memory.memory_id == first.memory.memory_id
    assert duplicate.memory.revision == 2

    replacement = service.remember(
        tenant_id="tenant_test",
        user_id="user_test",
        topic="output.language.default",
        content="Use Chinese output.",
        supporting_message_ids=[message_id],
        logical_key="remember-3",
    )
    assert replacement.disposition == "replacement"
    assert replacement.memory.related_memory_id == first.memory.memory_id

    conflict = service.remember(
        tenant_id="tenant_test",
        user_id="user_test",
        topic="output.language.default",
        content="Use bilingual output.",
        supporting_message_ids=[message_id],
        logical_key="turn-1",
        source="turn_completion",
        confidence=0.9,
    )
    assert conflict.disposition == "pending_conflict"
    assert conflict.memory.related_memory_id == replacement.memory.memory_id
    assert service.recall(
        tenant_id="tenant_test", user_id="user_test", planning_query="any research"
    ).items[0].memory_id == replacement.memory.memory_id

    accepted = service.resolve(
        tenant_id="tenant_test",
        user_id="user_test",
        memory_id=conflict.memory.memory_id,
        action="accept_new",
        expected_revision=1,
        related_expected_revision=1,
    )
    assert accepted.status == "active"
    assert accepted.revision == 2
    replayed_resolution = service.resolve(
        tenant_id="tenant_test",
        user_id="user_test",
        memory_id=conflict.memory.memory_id,
        action="accept_new",
        expected_revision=1,
        related_expected_revision=1,
    )
    assert replayed_resolution.memory_id == accepted.memory_id
    assert replayed_resolution.status == accepted.status
    assert replayed_resolution.revision == accepted.revision
    forgotten = service.forget(
        tenant_id="tenant_test",
        user_id="user_test",
        memory_id=accepted.memory_id,
        expected_revision=accepted.revision,
    )
    assert forgotten.status == "forgotten"
    assert service.recall(
        tenant_id="tenant_test", user_id="user_test", planning_query="output language"
    ).items == []
    assert service.list(
        tenant_id="tenant_test", user_id="another_user", status="active"
    ) == []


def test_memory_manual_edit_cas_write_identity_and_embedding_worker(session_factory) -> None:
    container = phase5_container(session_factory)
    message_id = create_supporting_message(container, "phase5-edit")
    service = container.memory_service
    created = service.remember(
        tenant_id="tenant_test",
        user_id="user_test",
        topic="research.source.preference",
        content="Prefer primary papers over blogs.",
        supporting_message_ids=[message_id],
        logical_key="remember-source",
    )

    try:
        service.remember(
            tenant_id="tenant_test",
            user_id="user_test",
            topic="research.source.preference",
            content="Prefer secondary blogs.",
            supporting_message_ids=[message_id],
            logical_key="remember-source",
        )
        raise AssertionError("changed idempotent payload must conflict")
    except IdempotencyConflictError:
        pass

    edited = service.manual_edit(
        tenant_id="tenant_test",
        user_id="user_test",
        memory_id=created.memory.memory_id,
        expected_revision=1,
        content="Prefer peer-reviewed primary papers.",
        logical_key="manual-1",
    )
    assert edited.memory.source == "manual_edit"
    assert edited.memory.related_memory_id == created.memory.memory_id
    try:
        service.manual_edit(
            tenant_id="tenant_test",
            user_id="user_test",
            memory_id=created.memory.memory_id,
            expected_revision=1,
            content="stale edit",
            logical_key="manual-stale",
        )
        raise AssertionError("stale edit must conflict")
    except StaleConflictError:
        pass

    with session_factory() as session:
        outbox = session.scalar(
            select(OutboxMessageRow).where(
                OutboxMessageRow.kind == "memory.embedding.requested",
                OutboxMessageRow.aggregate_id == edited.memory.memory_id,
            )
        )
        assert outbox is not None
        payload = dict(outbox.payload_json)
    assert container.memory_embedding_worker.run(payload) is True
    with session_factory() as session:
        row = session.scalar(
            select(ResearchMemoryRow).where(
                ResearchMemoryRow.memory_id == edited.memory.memory_id
            )
        )
        assert row is not None and row.embedding is not None
        assert len(row.embedding) == 32


def test_extract_batch_enforces_user_support_topic_category_and_confidence(session_factory) -> None:
    container = phase5_container(session_factory)
    message_id = create_supporting_message(container, "phase5-extract")
    service = container.memory_service
    candidates = [
        MemoryCandidate(
            topic="source preference",
            category="research_preference",
            content="Prefer primary papers.",
            confidence=0.92,
            supporting_user_message_ids=[message_id],
        ),
        MemoryCandidate(
            topic="unknown.topic",
            category="research_preference",
            content="Made up namespace.",
            confidence=0.99,
            supporting_user_message_ids=[message_id],
        ),
        MemoryCandidate(
            topic="output language",
            category="output_preference",
            content="Use German.",
            confidence=0.69,
            supporting_user_message_ids=[message_id],
        ),
    ]
    results = service.extract_batch(
        tenant_id="tenant_test",
        user_id="user_test",
        candidates=candidates,
        user_messages={message_id: "Remember that I prefer primary papers."},
        input_hash="a" * 64,
    )
    assert len(results) == 1
    assert results[0].memory.source == "turn_completion"
    with session_factory() as session:
        assert session.scalar(select(func.count()).select_from(ResearchMemoryRow)) == 1


def test_memory_management_api_is_scoped_and_returns_stale_conflict(session_factory) -> None:
    container = phase5_container(session_factory)
    client = TestClient(create_app(container))
    task = client.post(
        "/api/v1/tasks",
        headers={"Idempotency-Key": "memory-api-task"},
        json={"query": "Remember that I prefer English output."},
    )
    assert task.status_code == 201
    message_id = task.json()["message_id"]
    remembered = client.post(
        "/api/v1/memories",
        headers={"Idempotency-Key": "memory-api-remember"},
        json={
            "topic": "output language",
            "content": "Prefer English output.",
            "supporting_message_ids": [message_id],
        },
    )
    assert remembered.status_code == 201
    memory = remembered.json()["memory"]
    assert client.get("/api/v1/memories").json()["items"][0]["memory_id"] == memory[
        "memory_id"
    ]
    assert client.get(
        "/api/v1/memories", headers={"X-User-ID": "another_user"}
    ).json()["items"] == []
    edited = client.patch(
        f"/api/v1/memories/{memory['memory_id']}",
        headers={"Idempotency-Key": "memory-api-edit"},
        json={"expected_revision": 1, "content": "Prefer concise English output."},
    )
    assert edited.status_code == 200
    stale = client.patch(
        f"/api/v1/memories/{memory['memory_id']}",
        headers={"Idempotency-Key": "memory-api-stale"},
        json={"expected_revision": 1, "content": "stale overwrite"},
    )
    assert stale.status_code == 409
    assert stale.json()["code"] == "stale_conflict"


def test_recall_unifies_routes_caps_top_k_and_rehydrate_invalidates_old_refs(
    session_factory,
) -> None:
    container = phase5_container(session_factory)
    message_id = create_supporting_message(container, "phase5-recall")
    topics = [item.topic for item in container.memory_service.topics.definitions]
    for index, topic in enumerate(topics):
        container.memory_service.remember(
            tenant_id="tenant_test",
            user_id="user_test",
            topic=topic,
            content=f"Research preference {index} for {topic}.",
            supporting_message_ids=[message_id],
            logical_key=f"recall-{index}",
        )
    context = container.memory_service.recall(
        tenant_id="tenant_test",
        user_id="user_test",
        planning_query=" ".join(topics),
    )
    assert len(context.items) == 8
    assert all(set(item.model_dump()) == {"memory_id", "revision", "content_hash"} for item in context.items)
    rendered = container.memory_service.rehydrate(
        "tenant_test", "user_test", context
    )
    assert len(rendered) == 8
    forgotten = context.items[0]
    container.memory_service.forget(
        tenant_id="tenant_test",
        user_id="user_test",
        memory_id=forgotten.memory_id,
        expected_revision=forgotten.revision,
    )
    refreshed = container.memory_service.rehydrate(
        "tenant_test", "user_test", context
    )
    assert len(refreshed) == 7
    assert all(item["memory_id"] != forgotten.memory_id for item in refreshed)


def test_memory_scope_deletion_is_independent_from_task_lifecycle(session_factory) -> None:
    container = phase5_container(session_factory)
    message_id = create_supporting_message(container, "phase5-lifecycle")
    memory = container.memory_service.remember(
        tenant_id="tenant_test",
        user_id="user_test",
        topic="output language",
        content="Use English output.",
        supporting_message_ids=[message_id],
        logical_key="lifecycle-memory",
    ).memory
    assert container.memory_service.delete_scope(
        tenant_id="tenant_test", user_id="another_user"
    ) == 0
    assert container.memory_service.list(
        tenant_id="tenant_test", user_id="user_test"
    )[0].memory_id == memory.memory_id
    assert container.memory_service.delete_scope(
        tenant_id="tenant_test", user_id="user_test"
    ) == 1
    assert container.memory_service.list(
        tenant_id="tenant_test", user_id="user_test"
    ) == []


class _MemoryJobs:
    def __init__(self) -> None:
        self.extractions: list[dict[str, object]] = []

    def publish_research(self, task_id: str) -> None:
        raise AssertionError(task_id)

    def publish_document(self, doc_id: str) -> None:
        raise AssertionError(doc_id)

    def publish_database_write(self, write_id: str, plan_hash: str) -> None:
        raise AssertionError((write_id, plan_hash))

    def publish_memory_extraction(self, payload: dict[str, object]) -> None:
        self.extractions.append(payload)

    def publish_memory_embedding(self, payload: dict[str, object]) -> None:
        raise AssertionError(payload)

    def publish_memory_gc(self) -> None:
        raise AssertionError("unexpected gc")


def test_memory_extraction_outbox_debounces_same_thread(session_factory) -> None:
    with session_factory() as session, session.begin():
        for sequence in (1, 2):
            session.add(
                OutboxMessageRow(
                    message_id=new_id("outbox"),
                    tenant_id="tenant_test",
                    kind="memory.extraction.requested",
                    aggregate_id="thread-shared",
                    payload_json={"sequence": sequence},
                    idempotency_key=f"memory-extraction-{sequence}",
                )
            )
    jobs = _MemoryJobs()
    dispatcher = OutboxDispatcher(session_factory, jobs, InMemoryEventBroker())
    assert dispatcher.dispatch_once() == 2
    assert jobs.extractions == [{"sequence": 2}]
    assert dispatcher.dispatch_once() == 0
