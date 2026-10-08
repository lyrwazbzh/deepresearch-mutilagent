from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import func, select

from src.agent.backends.persistence.models import (
    ConversationMessageRow,
    OutboxMessageRow,
    ResearchCheckpointRow,
    ResearchTaskRow,
)
from src.agent.errors import IdempotencyConflictError
from src.agent.task_service import CreateTaskRequest


def test_create_task_is_atomic_and_idempotent(make_container, session_factory) -> None:
    container, _ = make_container({})
    request = CreateTaskRequest(query="original text", options={"max_depth": 99})
    created = container.task_service.create("tenant_test", "user_test", "key-1", request)
    assert created.projection.task_status.value == "pending"
    assert created.projection.message_id is not None

    replay = container.task_service.create("tenant_test", "user_test", "key-1", request)
    assert replay.replayed is True
    assert replay.projection.task_id == created.projection.task_id

    with pytest.raises(IdempotencyConflictError):
        container.task_service.create(
            "tenant_test", "user_test", "key-1", CreateTaskRequest(query="different")
        )

    with session_factory() as session:
        assert session.scalar(select(func.count()).select_from(ResearchTaskRow)) == 1
        message = session.scalar(select(ConversationMessageRow))
        assert message is not None and message.content == "original text"
        task = session.scalar(select(ResearchTaskRow))
        assert task is not None and task.latest_state["execution"]["max_depth"] == 5
        checkpoint = session.scalar(select(ResearchCheckpointRow))
        assert checkpoint is not None and checkpoint.checkpoint_seq == 0
        outbox = session.scalar(select(OutboxMessageRow))
        assert outbox is not None and outbox.kind == "research.schedule"
        assert task.model_binding_snapshot["bindings"]["routing"]["primary"]["version"] == 1


def test_concurrent_same_key_creates_one_logical_task(make_container, session_factory) -> None:
    container, _ = make_container({})
    request = CreateTaskRequest(query="same")

    def create():
        return container.task_service.create("tenant_test", "user_test", "same-key", request)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: create(), range(2)))
    assert len({result.projection.task_id for result in results}) == 1
    with session_factory() as session:
        assert session.scalar(select(func.count()).select_from(ResearchTaskRow)) == 1
        assert session.scalar(select(func.count()).select_from(ConversationMessageRow)) == 1
        assert session.scalar(select(func.count()).select_from(ResearchCheckpointRow)) == 1
        assert session.scalar(select(func.count()).select_from(OutboxMessageRow)) == 1

