from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import timedelta

import pytest

from src.agent.backends.events import EventRetentionCleaner, EventService, InMemoryEventBroker
from src.agent.backends.persistence.models import ResearchEventRow, ResearchTaskRow, utcnow
from src.agent.errors import EventCursorExpiredError
from src.agent.task_service import CreateTaskRequest


def _task(container, key: str) -> str:
    return container.task_service.create(
        "tenant_test",
        "user_test",
        key,
        CreateTaskRequest(query="SSE contract"),
    ).projection.task_id


def _event(session_factory, task_id: str, changed: list[str]) -> int:
    with session_factory() as session, session.begin():
        row = ResearchEventRow(
            tenant_id="tenant_test",
            task_id=task_id,
            event_type="task_updated",
            payload_json={
                "event_id": 0,
                "task_id": task_id,
                "changed": changed,
                "at": utcnow().isoformat(),
            },
        )
        session.add(row)
        session.flush()
        row.payload_json = {**row.payload_json, "event_id": row.event_id}
        return row.event_id


@pytest.mark.asyncio
async def test_sse_replay_live_handoff_heartbeat_and_disconnect(
    make_container, session_factory
) -> None:
    container, _ = make_container({})
    task_id = _task(container, "phase10-sse-handoff")
    first = _event(session_factory, task_id, ["task_status"])
    broker = InMemoryEventBroker(heartbeat_seconds=0.01)
    service = EventService(session_factory, broker)
    stream = service.stream("tenant_test", task_id, 0)

    replay = await anext(stream)
    assert f"id: {first}" in replay
    assert "event: task_updated" in replay

    heartbeat = await asyncio.wait_for(anext(stream), timeout=1)
    assert heartbeat == ": heartbeat\n\n"

    second = _event(session_factory, task_id, ["phase"])
    await broker.publish("tenant_test", task_id, second)
    live = await asyncio.wait_for(anext(stream), timeout=1)
    assert f"id: {second}" in live
    assert "\"changed\":[\"phase\"]" in live
    await stream.aclose()

    replayed = service.replay("tenant_test", task_id, first)
    assert [event["event_id"] for event in replayed] == [second]


@pytest.mark.asyncio
async def test_sse_subscription_is_consumed_and_closed_by_one_task(
    make_container, session_factory
) -> None:
    container, _ = make_container({})
    task_id = _task(container, "phase10-sse-single-owner")

    class TaskOwnedBroker:
        def __init__(self) -> None:
            self.owner: asyncio.Task[object] | None = None
            self.consumer: asyncio.Task[object] | None = None
            self.closed_by: asyncio.Task[object] | None = None

        async def publish(self, tenant_id: str, task_id: str, event_id: int) -> None:
            del tenant_id, task_id, event_id

        @asynccontextmanager
        async def subscribe(
            self, tenant_id: str, task_id: str
        ) -> AsyncIterator[AsyncIterator[int]]:
            del tenant_id, task_id
            self.owner = asyncio.current_task()

            async def notifications() -> AsyncIterator[int]:
                self.consumer = asyncio.current_task()
                while True:
                    await asyncio.sleep(0.01)
                    yield 0

            try:
                yield notifications()
            finally:
                self.closed_by = asyncio.current_task()

    broker = TaskOwnedBroker()
    stream = EventService(session_factory, broker).stream("tenant_test", task_id, 0)
    assert await asyncio.wait_for(anext(stream), timeout=1) == ": heartbeat\n\n"
    await stream.aclose()

    assert broker.owner is broker.consumer
    assert broker.closed_by is broker.owner


@pytest.mark.asyncio
async def test_sse_slow_consumer_gets_non_persistent_resync_control(
    make_container, session_factory
) -> None:
    container, _ = make_container({})
    task_id = _task(container, "phase10-sse-overflow")
    broker = InMemoryEventBroker(heartbeat_seconds=5)
    service = EventService(
        session_factory,
        broker,
        client_buffer_events_max=1,
        client_buffer_bytes_max=64,
    )
    stream = service.stream("tenant_test", task_id, 0)
    pending = asyncio.create_task(anext(stream))
    while not broker.queues[("tenant_test", task_id)]:
        await asyncio.sleep(0)
    await broker.publish("tenant_test", task_id, 101)
    await broker.publish("tenant_test", task_id, 102)
    control = await asyncio.wait_for(pending, timeout=1)
    assert "event: resync_required" in control
    await stream.aclose()

    with session_factory() as session:
        persisted = list(
            session.query(ResearchEventRow).filter_by(task_id=task_id).all()
        )
    assert persisted == []


def test_event_retention_and_expired_cursor_use_postgres_truth(
    make_container, session_factory
) -> None:
    container, _ = make_container({})
    task_id = _task(container, "phase10-sse-retention")
    event_id = _event(session_factory, task_id, ["task_status"])
    old = utcnow() - timedelta(days=8)
    with session_factory() as session, session.begin():
        task = session.get(ResearchTaskRow, task_id)
        assert task is not None
        task.status = "completed"
        task.updated_at = old
        event = session.get(ResearchEventRow, event_id)
        assert event is not None
        event.created_at = old

    cleaner = EventRetentionCleaner(session_factory, retention_days=7)
    assert cleaner.prune_once(now=utcnow()) == 1
    service = EventService(session_factory, InMemoryEventBroker())
    with pytest.raises(EventCursorExpiredError):
        service.validate_cursor("tenant_test", task_id, event_id)


@pytest.mark.asyncio
async def test_sse_connection_limits_are_tenant_and_user_scoped(
    make_container, session_factory
) -> None:
    container, _ = make_container({})
    task_id = _task(container, "phase10-sse-limits")
    service = EventService(
        session_factory,
        InMemoryEventBroker(),
        connections_per_task_max=1,
        connections_per_user_max=1,
    )
    await service.acquire_connection("tenant_test", "user_test", task_id)
    with pytest.raises(Exception) as error:
        await service.acquire_connection("tenant_test", "user_test", task_id)
    assert getattr(error.value, "code", None) == "rate_limited"
    await service.release_connection("tenant_test", "user_test", task_id)
    await service.acquire_connection("tenant_test", "user_test", task_id)
    await service.release_connection("tenant_test", "user_test", task_id)
