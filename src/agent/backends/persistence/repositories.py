from __future__ import annotations

import builtins
from datetime import datetime

from sqlalchemy import Select, func, select
from sqlalchemy.orm import Session

from src.agent.backends.persistence.models import (
    ConversationMessageRow,
    ModelBindingRow,
    ModelConfigHeadRow,
    ModelConfigRevocationRow,
    ModelConfigRow,
    NodeRunRow,
    OutboxMessageRow,
    ResearchMemoryRow,
    ResearchCheckpointRow,
    ResearchEventRow,
    ResearchTaskRow,
    ThreadRow,
)


class ThreadRepository:
    def __init__(self, session: Session, tenant_id: str, user_id: str) -> None:
        self.session = session
        self.tenant_id = tenant_id
        self.user_id = user_id

    def get(self, thread_id: str) -> ThreadRow | None:
        return self.session.scalar(
            select(ThreadRow).where(
                ThreadRow.thread_id == thread_id,
                ThreadRow.tenant_id == self.tenant_id,
                ThreadRow.user_id == self.user_id,
            )
        )

    def add(self, row: ThreadRow) -> None:
        if row.tenant_id != self.tenant_id or row.user_id != self.user_id:
            raise ValueError("thread scope mismatch")
        self.session.add(row)


class MessageRepository:
    def __init__(self, session: Session, tenant_id: str, user_id: str) -> None:
        self.session = session
        self.tenant_id = tenant_id
        self.user_id = user_id

    def recent(self, thread_id: str, limit: int = 12) -> list[ConversationMessageRow]:
        rows = self.session.scalars(
            select(ConversationMessageRow)
            .where(
                ConversationMessageRow.tenant_id == self.tenant_id,
                ConversationMessageRow.user_id == self.user_id,
                ConversationMessageRow.thread_id == thread_id,
            )
            .order_by(ConversationMessageRow.created_at.desc(), ConversationMessageRow.message_id.desc())
            .limit(limit)
        ).all()
        return list(reversed(rows))

    def add(self, row: ConversationMessageRow) -> None:
        if row.tenant_id != self.tenant_id or row.user_id != self.user_id:
            raise ValueError("message scope mismatch")
        self.session.add(row)


class TaskRepository:
    def __init__(self, session: Session, tenant_id: str, user_id: str | None = None) -> None:
        self.session = session
        self.tenant_id = tenant_id
        self.user_id = user_id

    def _scoped(self) -> Select[tuple[ResearchTaskRow]]:
        stmt = select(ResearchTaskRow).where(ResearchTaskRow.tenant_id == self.tenant_id)
        if self.user_id is not None:
            stmt = stmt.where(ResearchTaskRow.user_id == self.user_id)
        return stmt

    def get(self, task_id: str, *, for_update: bool = False) -> ResearchTaskRow | None:
        stmt = self._scoped().where(ResearchTaskRow.task_id == task_id)
        if for_update:
            stmt = stmt.with_for_update()
        return self.session.scalar(stmt)

    def by_idempotency_key(self, key: str) -> ResearchTaskRow | None:
        if self.user_id is None:
            raise ValueError("user scope is required for idempotency lookup")
        return self.session.scalar(
            self._scoped().where(ResearchTaskRow.create_idempotency_key == key)
        )

    def add(self, row: ResearchTaskRow) -> None:
        if row.tenant_id != self.tenant_id:
            raise ValueError("task tenant scope mismatch")
        if self.user_id is not None and row.user_id != self.user_id:
            raise ValueError("task user scope mismatch")
        self.session.add(row)


class MemoryRepository:
    """Every Memory query is tenant + user scoped at SQL construction time."""

    def __init__(self, session: Session, tenant_id: str, user_id: str) -> None:
        self.session = session
        self.tenant_id = tenant_id
        self.user_id = user_id

    def _scoped(self) -> Select[tuple[ResearchMemoryRow]]:
        return select(ResearchMemoryRow).where(
            ResearchMemoryRow.tenant_id == self.tenant_id,
            ResearchMemoryRow.user_id == self.user_id,
        )

    def get(self, memory_id: str, *, for_update: bool = False) -> ResearchMemoryRow | None:
        statement = self._scoped().where(ResearchMemoryRow.memory_id == memory_id)
        if for_update:
            statement = statement.with_for_update()
        return self.session.scalar(statement)

    def by_write_key(self, write_key: str) -> ResearchMemoryRow | None:
        return self.session.scalar(
            self._scoped().where(ResearchMemoryRow.write_key == write_key)
        )

    def exact_live(self, topic: str, content_hash: str) -> ResearchMemoryRow | None:
        return self.session.scalar(
            self._scoped().where(
                ResearchMemoryRow.topic == topic,
                ResearchMemoryRow.content_hash == content_hash,
                ResearchMemoryRow.status.in_(("active", "pending_conflict")),
            )
        )

    def active_topic(self, topic: str, *, for_update: bool = False) -> ResearchMemoryRow | None:
        statement = self._scoped().where(
            ResearchMemoryRow.topic == topic,
            ResearchMemoryRow.status == "active",
        )
        if for_update:
            statement = statement.with_for_update()
        return self.session.scalar(statement)

    def list(
        self,
        *,
        status: str | None = None,
        topic: str | None = None,
        limit: int = 100,
    ) -> builtins.list[ResearchMemoryRow]:
        statement = self._scoped()
        if status is not None:
            statement = statement.where(ResearchMemoryRow.status == status)
        if topic is not None:
            statement = statement.where(ResearchMemoryRow.topic == topic)
        return list(
            self.session.scalars(
                statement.order_by(
                    ResearchMemoryRow.updated_at.desc(), ResearchMemoryRow.memory_id
                ).limit(limit)
            ).all()
        )

    def active(self) -> builtins.list[ResearchMemoryRow]:
        return list(
            self.session.scalars(
                self._scoped()
                .where(ResearchMemoryRow.status == "active")
                .order_by(ResearchMemoryRow.updated_at.desc(), ResearchMemoryRow.memory_id)
            ).all()
        )

    def add(self, row: ResearchMemoryRow) -> None:
        if row.tenant_id != self.tenant_id or row.user_id != self.user_id:
            raise ValueError("memory scope mismatch")
        self.session.add(row)


class CheckpointRepository:
    def __init__(self, session: Session, tenant_id: str, task_id: str) -> None:
        self.session = session
        self.tenant_id = tenant_id
        self.task_id = task_id

    def latest(self) -> ResearchCheckpointRow | None:
        return self.session.scalar(
            select(ResearchCheckpointRow)
            .where(
                ResearchCheckpointRow.tenant_id == self.tenant_id,
                ResearchCheckpointRow.task_id == self.task_id,
            )
            .order_by(ResearchCheckpointRow.checkpoint_seq.desc())
            .limit(1)
        )

    def next_seq(self) -> int:
        value = self.session.scalar(
            select(func.max(ResearchCheckpointRow.checkpoint_seq)).where(
                ResearchCheckpointRow.tenant_id == self.tenant_id,
                ResearchCheckpointRow.task_id == self.task_id,
            )
        )
        return int(value or 0) + 1


class NodeRunRepository:
    def __init__(self, session: Session, tenant_id: str, task_id: str) -> None:
        self.session = session
        self.tenant_id = tenant_id
        self.task_id = task_id

    def by_semantic_key(self, node_key: str, input_checkpoint_id: str) -> NodeRunRow | None:
        return self.session.scalar(
            select(NodeRunRow).where(
                NodeRunRow.tenant_id == self.tenant_id,
                NodeRunRow.task_id == self.task_id,
                NodeRunRow.node_key == node_key,
                NodeRunRow.input_checkpoint_id == input_checkpoint_id,
            )
        )

    def get(self, node_run_id: str, *, for_update: bool = False) -> NodeRunRow | None:
        stmt = select(NodeRunRow).where(
            NodeRunRow.tenant_id == self.tenant_id,
            NodeRunRow.task_id == self.task_id,
            NodeRunRow.node_run_id == node_run_id,
        )
        if for_update:
            stmt = stmt.with_for_update()
        return self.session.scalar(stmt)


class EventRepository:
    def __init__(self, session: Session, tenant_id: str, task_id: str) -> None:
        self.session = session
        self.tenant_id = tenant_id
        self.task_id = task_id

    def after(self, event_id: int, limit: int = 500) -> list[ResearchEventRow]:
        return list(
            self.session.scalars(
                select(ResearchEventRow)
                .where(
                    ResearchEventRow.tenant_id == self.tenant_id,
                    ResearchEventRow.task_id == self.task_id,
                    ResearchEventRow.event_id > event_id,
                )
                .order_by(ResearchEventRow.event_id)
                .limit(limit)
            ).all()
        )

    def bounds(self) -> tuple[int | None, int | None]:
        row = self.session.execute(
            select(
                func.min(ResearchEventRow.event_id),
                func.max(ResearchEventRow.event_id),
            ).where(
                ResearchEventRow.tenant_id == self.tenant_id,
                ResearchEventRow.task_id == self.task_id,
            )
        ).one()
        return row[0], row[1]


class OutboxRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def pending(self, now: datetime, limit: int = 100) -> list[OutboxMessageRow]:
        return list(
            self.session.scalars(
                select(OutboxMessageRow)
                .where(
                    OutboxMessageRow.dispatched_at.is_(None),
                    OutboxMessageRow.available_at <= now,
                )
                .order_by(OutboxMessageRow.created_at)
                .limit(limit)
                .with_for_update(skip_locked=True)
            ).all()
        )


class ModelConfigRepository:
    def __init__(self, session: Session, tenant_id: str) -> None:
        self.session = session
        self.tenant_id = tenant_id

    def bindings(self, purpose: str = "chat") -> list[tuple[ModelBindingRow, ModelConfigHeadRow]]:
        return [
            (binding, head)
            for binding, head in self.session.execute(
                select(ModelBindingRow, ModelConfigHeadRow)
                .join(
                    ModelConfigHeadRow,
                    (ModelConfigHeadRow.tenant_id == ModelBindingRow.tenant_id)
                    & (ModelConfigHeadRow.model_config_id == ModelBindingRow.model_config_id),
                )
                .where(
                    ModelBindingRow.tenant_id == self.tenant_id,
                    ModelBindingRow.purpose == purpose,
                    ModelConfigHeadRow.enabled_for_new_tasks.is_(True),
                )
                .order_by(ModelBindingRow.binding_key, ModelBindingRow.priority)
            ).all()
        ]

    def revision(self, model_config_id: str, version: int) -> ModelConfigRow | None:
        return self.session.scalar(
            select(ModelConfigRow).where(
                ModelConfigRow.tenant_id == self.tenant_id,
                ModelConfigRow.model_config_id == model_config_id,
                ModelConfigRow.version == version,
            )
        )

    def is_revoked(self, model_config_id: str, version: int) -> bool:
        return (
            self.session.scalar(
                select(ModelConfigRevocationRow).where(
                    ModelConfigRevocationRow.tenant_id == self.tenant_id,
                    ModelConfigRevocationRow.model_config_id == model_config_id,
                    ModelConfigRevocationRow.version == version,
                )
            )
            is not None
        )
