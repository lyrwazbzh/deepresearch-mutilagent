from __future__ import annotations

import asyncio
import json
import logging
from collections import defaultdict
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from datetime import datetime, timedelta, timezone
from typing import AsyncContextManager, Protocol

from redis.asyncio import Redis
from sqlalchemy import and_, delete, or_, select

from src.agent.backends.persistence.database import SessionFactory
from src.agent.backends.persistence.models import ResearchEventRow, ResearchTaskRow
from src.agent.backends.persistence.repositories import EventRepository, TaskRepository
from src.agent.errors import EventCursorExpiredError, NotFoundError, RateLimitedError


logger = logging.getLogger("uvicorn.error")


class EventBroker(Protocol):
    async def publish(self, tenant_id: str, task_id: str, event_id: int) -> None: ...

    def subscribe(
        self, tenant_id: str, task_id: str
    ) -> AsyncContextManager[AsyncIterator[int]]: ...


class RedisEventBroker:
    def __init__(self, redis_url: str, *, heartbeat_seconds: int = 15) -> None:
        self.redis = Redis.from_url(redis_url, decode_responses=True)
        self.heartbeat_seconds = heartbeat_seconds

    @staticmethod
    def channel(tenant_id: str, task_id: str) -> str:
        return f"{tenant_id}:sse:task:{task_id}"

    async def publish(self, tenant_id: str, task_id: str, event_id: int) -> None:
        await self.redis.publish(self.channel(tenant_id, task_id), str(event_id))

    @asynccontextmanager
    async def subscribe(self, tenant_id: str, task_id: str) -> AsyncIterator[AsyncIterator[int]]:
        pubsub = self.redis.pubsub()
        await pubsub.subscribe(self.channel(tenant_id, task_id))

        async def messages() -> AsyncIterator[int]:
            while True:
                item = await pubsub.get_message(
                    ignore_subscribe_messages=True,
                    timeout=float(self.heartbeat_seconds),
                )
                if item is None:
                    yield 0
                else:
                    yield int(item["data"])

        try:
            yield messages()
        finally:
            await pubsub.unsubscribe(self.channel(tenant_id, task_id))
            await pubsub.aclose()


class InMemoryEventBroker:
    def __init__(self, *, heartbeat_seconds: int = 15) -> None:
        self.queues: dict[tuple[str, str], list[asyncio.Queue[int]]] = defaultdict(list)
        self.heartbeat_seconds = heartbeat_seconds

    async def publish(self, tenant_id: str, task_id: str, event_id: int) -> None:
        for queue in list(self.queues[(tenant_id, task_id)]):
            await queue.put(event_id)

    @asynccontextmanager
    async def subscribe(self, tenant_id: str, task_id: str) -> AsyncIterator[AsyncIterator[int]]:
        key = (tenant_id, task_id)
        queue: asyncio.Queue[int] = asyncio.Queue()
        self.queues[key].append(queue)

        async def messages() -> AsyncIterator[int]:
            while True:
                try:
                    yield await asyncio.wait_for(
                        queue.get(), timeout=self.heartbeat_seconds
                    )
                except TimeoutError:
                    yield 0

        try:
            yield messages()
        finally:
            self.queues[key].remove(queue)


class EventService:
    def __init__(
        self,
        session_factory: SessionFactory,
        broker: EventBroker,
        *,
        connections_per_task_max: int = 5,
        connections_per_user_max: int = 20,
        client_buffer_events_max: int = 256,
        client_buffer_bytes_max: int = 1_048_576,
        retention_days: int = 7,
    ) -> None:
        self.session_factory = session_factory
        self.broker = broker
        self.connections_per_task_max = connections_per_task_max
        self.connections_per_user_max = connections_per_user_max
        self.client_buffer_events_max = client_buffer_events_max
        self.client_buffer_bytes_max = client_buffer_bytes_max
        self.retention_days = retention_days
        self._connection_lock = asyncio.Lock()
        self._task_connections: dict[tuple[str, str], int] = defaultdict(int)
        self._user_connections: dict[tuple[str, str], int] = defaultdict(int)

    def assert_task(self, tenant_id: str, user_id: str, task_id: str) -> None:
        with self.session_factory() as session:
            row = TaskRepository(session, tenant_id, user_id).get(task_id)
            if row is None or row.deleted_at is not None:
                raise NotFoundError("Task was not found.")

    def replay(self, tenant_id: str, task_id: str, after_id: int) -> list[dict[str, object]]:
        with self.session_factory() as session:
            repository = EventRepository(session, tenant_id, task_id)
            events: list[dict[str, object]] = []
            cursor = after_id
            while True:
                rows = repository.after(cursor)
                events.extend(
                    {
                        "event_id": row.event_id,
                        "event": row.event_type,
                        "data": row.payload_json,
                    }
                    for row in rows
                )
                if len(rows) < 500:
                    return events
                cursor = rows[-1].event_id

    def validate_cursor(self, tenant_id: str, task_id: str, after_id: int) -> None:
        if after_id == 0:
            return
        with self.session_factory() as session:
            earliest, _latest = EventRepository(session, tenant_id, task_id).bounds()
            task = session.scalar(
                select(ResearchTaskRow).where(
                    ResearchTaskRow.tenant_id == tenant_id,
                    ResearchTaskRow.task_id == task_id,
                )
            )
        task_updated_at = (
            task.updated_at.replace(tzinfo=timezone.utc)
            if task is not None and task.updated_at.tzinfo is None
            else task.updated_at if task is not None else None
        )
        expired_empty_history = bool(
            earliest is None
            and task is not None
            and task_updated_at is not None
            and task.status in {"cancelled", "completed", "failed"}
            and task_updated_at
            < datetime.now(timezone.utc) - timedelta(days=self.retention_days)
        )
        if expired_empty_history or (earliest is not None and after_id < earliest):
            raise EventCursorExpiredError(
                "The event cursor is older than the retained task event history."
            )

    async def acquire_connection(self, tenant_id: str, user_id: str, task_id: str) -> None:
        task_key = (tenant_id, task_id)
        user_key = (tenant_id, user_id)
        async with self._connection_lock:
            if (
                self._task_connections[task_key] >= self.connections_per_task_max
                or self._user_connections[user_key] >= self.connections_per_user_max
            ):
                raise RateLimitedError("The SSE connection limit has been reached.")
            self._task_connections[task_key] += 1
            self._user_connections[user_key] += 1
            logger.info(
                "sse_connected",
                extra={"tenant_id": tenant_id, "user_id": user_id, "task_id": task_id},
            )

    async def release_connection(self, tenant_id: str, user_id: str, task_id: str) -> None:
        task_key = (tenant_id, task_id)
        user_key = (tenant_id, user_id)
        async with self._connection_lock:
            self._task_connections[task_key] = max(0, self._task_connections[task_key] - 1)
            self._user_connections[user_key] = max(0, self._user_connections[user_key] - 1)
            logger.info(
                "sse_disconnected",
                extra={"tenant_id": tenant_id, "user_id": user_id, "task_id": task_id},
            )

    async def stream(
        self, tenant_id: str, task_id: str, after_id: int
    ) -> AsyncIterator[str]:
        cursor = after_id
        queue: asyncio.Queue[tuple[int, int]] = asyncio.Queue(
            self.client_buffer_events_max
        )
        overflow = asyncio.Event()
        subscribed: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        queued_bytes = 0

        async def pump() -> None:
            nonlocal queued_bytes
            try:
                # The same task owns the subscription context and consumes its iterator.
                # This avoids racing context-manager teardown with an in-flight anext()
                # when Starlette cancels a rapidly replaced browser stream.
                async with self.broker.subscribe(tenant_id, task_id) as notifications:
                    if not subscribed.done():
                        subscribed.set_result(None)
                    async for notified_id in notifications:
                        notification_bytes = len(str(notified_id).encode()) + 1
                        try:
                            if queued_bytes + notification_bytes > self.client_buffer_bytes_max:
                                raise asyncio.QueueFull
                            queue.put_nowait((notified_id, notification_bytes))
                            queued_bytes += notification_bytes
                        except asyncio.QueueFull:
                            overflow.set()
                            return
            except asyncio.CancelledError:
                if not subscribed.done():
                    subscribed.cancel()
                raise
            except Exception as exc:
                if not subscribed.done():
                    subscribed.set_exception(exc)
                else:
                    overflow.set()
                    with suppress(asyncio.QueueFull):
                        queue.put_nowait((0, 0))
                raise

        # Subscribe first, then replay PG. A notification is only a wake-up; PG remains authoritative.
        pump_task = asyncio.create_task(pump())
        try:
            await subscribed
            replayed = self.replay(tenant_id, task_id, cursor)
            logger.info(
                "sse_replay",
                extra={
                    "tenant_id": tenant_id,
                    "task_id": task_id,
                    "last_event_id": after_id,
                    "replay_count": len(replayed),
                },
            )
            for event in replayed:
                cursor = int(str(event["event_id"]))
                frame = self._format(event)
                if len(frame.encode()) > self.client_buffer_bytes_max:
                    logger.warning(
                        "sse_resync_required",
                        extra={"tenant_id": tenant_id, "task_id": task_id, "last_event_id": cursor},
                    )
                    yield self._resync_required(task_id, cursor)
                    return
                yield frame
            while True:
                if overflow.is_set():
                    logger.warning(
                        "sse_resync_required",
                        extra={
                            "tenant_id": tenant_id,
                            "task_id": task_id,
                            "last_event_id": cursor,
                        },
                    )
                    yield self._resync_required(task_id, cursor)
                    return
                notified_id, notification_bytes = await queue.get()
                queued_bytes = max(0, queued_bytes - notification_bytes)
                if notified_id == 0:
                    yield ": heartbeat\n\n"
                    continue
                for event in self.replay(tenant_id, task_id, cursor):
                    cursor = int(str(event["event_id"]))
                    frame = self._format(event)
                    if len(frame.encode()) > self.client_buffer_bytes_max:
                        yield self._resync_required(task_id, cursor)
                        return
                    yield frame
        finally:
            pump_task.cancel()
            with suppress(asyncio.CancelledError):
                await pump_task

    @staticmethod
    def _format(event: dict[str, object]) -> str:
        return (
            f"id: {event['event_id']}\n"
            f"event: {event['event']}\n"
            f"data: {json.dumps(event['data'], ensure_ascii=False, separators=(',', ':'))}\n\n"
        )

    @staticmethod
    def _resync_required(task_id: str, cursor: int) -> str:
        payload = json.dumps(
            {"task_id": task_id, "after_id": cursor},
            separators=(",", ":"),
        )
        return f"event: resync_required\ndata: {payload}\n\n"


class EventRetentionCleaner:
    """Prune only the frozen terminal/deleted Task event retention windows."""

    def __init__(self, session_factory: SessionFactory, retention_days: int = 7) -> None:
        self.session_factory = session_factory
        self.retention_days = retention_days

    def prune_once(self, now: datetime | None = None) -> int:
        current = now or datetime.now(timezone.utc)
        terminal_cutoff = current - timedelta(days=self.retention_days)
        deleted_cutoff = current - timedelta(hours=24)
        expired_tasks = select(ResearchTaskRow.task_id).where(
            or_(
                and_(
                    ResearchTaskRow.deleted_at.is_not(None),
                    ResearchTaskRow.deleted_at < deleted_cutoff,
                ),
                and_(
                    ResearchTaskRow.deleted_at.is_(None),
                    ResearchTaskRow.status.in_(["cancelled", "completed", "failed"]),
                    ResearchTaskRow.updated_at < terminal_cutoff,
                ),
            )
        )
        with self.session_factory() as session, session.begin():
            result = session.execute(
                delete(ResearchEventRow).where(ResearchEventRow.task_id.in_(expired_tasks))
            )
            return int(result.rowcount or 0)
