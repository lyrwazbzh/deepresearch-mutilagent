from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from src.agent.schema.task import TaskBudget
from src.agent.backends.persistence.database import SessionFactory
from src.agent.backends.persistence.models import (
    DocumentIngestionRow,
    OutboxMessageRow,
    ResearchEventRow,
    ResearchTaskRow,
    TaskKnowledgeGraphRow,
    utcnow,
)
from src.agent.task_service import new_id


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


class RuntimeRecoveryScanner:
    """Repairs expired activations and redelivers stranded pending tasks."""

    def __init__(
        self, session_factory: SessionFactory, *, pending_stale_seconds: int = 60
    ) -> None:
        self.session_factory = session_factory
        self.pending_stale_seconds = max(1, pending_stale_seconds)

    def scan_once(self, *, now: datetime | None = None) -> int:
        now = now or utcnow()
        repaired = 0
        with self.session_factory() as session, session.begin():
            expired = session.scalars(
                select(ResearchTaskRow)
                .where(
                    ResearchTaskRow.status.in_({"running", "pausing", "cancelling"}),
                    ResearchTaskRow.leased_until.is_not(None),
                    ResearchTaskRow.leased_until <= now,
                )
                .with_for_update()
            ).all()
            for task in expired:
                old_status = task.status
                old_lease = task.lease_token or "missing"
                self._settle_expired_wall_time(task, now)
                task.status = {
                    "running": "pending",
                    "pausing": "paused",
                    "cancelling": "cancelled",
                }[old_status]
                task.lease_token = None
                task.leased_until = None
                task.budget_accounted_at = None
                task.updated_at = now
                if task.status == "pending":
                    session.add(
                        OutboxMessageRow(
                            message_id=new_id("outbox"),
                            tenant_id=task.tenant_id,
                            kind="research.schedule",
                            aggregate_id=task.task_id,
                            payload_json={"task_id": task.task_id},
                            idempotency_key=f"recovery:{task.task_id}:{old_lease}",
                            available_at=now,
                        )
                    )
                self._event(session, task, now, old_status)
                repaired += 1

            cutoff = now - timedelta(seconds=self.pending_stale_seconds)
            pending = session.scalars(
                select(ResearchTaskRow)
                .where(
                    ResearchTaskRow.status == "pending",
                    ResearchTaskRow.updated_at <= cutoff,
                    ResearchTaskRow.deleted_at.is_(None),
                )
                .with_for_update()
            ).all()
            for task in pending:
                undispatched = session.scalar(
                    select(OutboxMessageRow.message_id).where(
                        OutboxMessageRow.kind == "research.schedule",
                        OutboxMessageRow.aggregate_id == task.task_id,
                        OutboxMessageRow.dispatched_at.is_(None),
                    )
                )
                if undispatched is not None:
                    continue
                bucket = int(now.timestamp()) // self.pending_stale_seconds
                session.add(
                    OutboxMessageRow(
                        message_id=new_id("outbox"),
                        tenant_id=task.tenant_id,
                        kind="research.schedule",
                        aggregate_id=task.task_id,
                        payload_json={"task_id": task.task_id},
                        idempotency_key=f"redelivery:{task.task_id}:{bucket}",
                        available_at=now,
                    )
                )
                task.updated_at = now
                repaired += 1
            repaired += self._recover_document_ingestions(session, now, cutoff)
            repaired += self._recover_knowledge_graph_builds(session, now, cutoff)
        return repaired

    def _recover_document_ingestions(
        self, session: object, now: datetime, cutoff: datetime
    ) -> int:
        repaired = 0
        rows = session.scalars(  # type: ignore[attr-defined]
            select(DocumentIngestionRow)
            .where(
                DocumentIngestionRow.status.in_({"queued", "processing"}),
                DocumentIngestionRow.updated_at <= cutoff,
            )
            .with_for_update()
        ).all()
        for row in rows:
            if row.status == "processing":
                row.status = "queued"
                row.updated_at = now
            undispatched = session.scalar(  # type: ignore[attr-defined]
                select(OutboxMessageRow.message_id).where(
                    OutboxMessageRow.kind == "document.ingest",
                    OutboxMessageRow.aggregate_id == row.doc_id,
                    OutboxMessageRow.dispatched_at.is_(None),
                )
            )
            if undispatched is None:
                bucket = int(now.timestamp()) // self.pending_stale_seconds
                session.add(  # type: ignore[attr-defined]
                    OutboxMessageRow(
                        message_id=new_id("outbox"),
                        tenant_id=row.tenant_id,
                        kind="document.ingest",
                        aggregate_id=row.doc_id,
                        payload_json={"doc_id": row.doc_id},
                        idempotency_key=(
                            f"recovery:{row.doc_id}:{row.generation}:"
                            f"{row.attempt_count}:{bucket}"
                        ),
                        available_at=now,
                    )
                )
                repaired += 1
        return repaired

    def _recover_knowledge_graph_builds(
        self, session: object, now: datetime, cutoff: datetime
    ) -> int:
        repaired = 0
        rows = session.scalars(  # type: ignore[attr-defined]
            select(TaskKnowledgeGraphRow)
            .where(
                TaskKnowledgeGraphRow.status == "building",
                TaskKnowledgeGraphRow.updated_at <= cutoff,
            )
            .with_for_update()
        ).all()
        for row in rows:
            undispatched = session.scalar(  # type: ignore[attr-defined]
                select(OutboxMessageRow.message_id).where(
                    OutboxMessageRow.kind == "knowledge_graph.build",
                    OutboxMessageRow.aggregate_id == row.task_id,
                    OutboxMessageRow.dispatched_at.is_(None),
                )
            )
            if undispatched is not None:
                continue
            bucket = int(now.timestamp()) // self.pending_stale_seconds
            session.add(  # type: ignore[attr-defined]
                OutboxMessageRow(
                    message_id=new_id("outbox"),
                    tenant_id=row.tenant_id,
                    kind="knowledge_graph.build",
                    aggregate_id=row.task_id,
                    payload_json={"task_id": row.task_id},
                    idempotency_key=f"kg-recovery:{row.task_id}:{bucket}",
                    available_at=now,
                )
            )
            row.updated_at = now
            repaired += 1
        return repaired

    @staticmethod
    def _settle_expired_wall_time(task: ResearchTaskRow, now: datetime) -> None:
        budget = TaskBudget.model_validate(task.task_budget)
        accounted = _aware(task.budget_accounted_at)
        leased_until = _aware(task.leased_until)
        if accounted is not None and leased_until is not None:
            settlement_end = min(now, leased_until)
            budget.usage.wall_time_ms += max(
                0, int((settlement_end - accounted).total_seconds() * 1000)
            )
        task.task_budget = budget.model_dump(mode="json")

    @staticmethod
    def _event(
        session: object,
        task: ResearchTaskRow,
        now: datetime,
        old_status: str,
    ) -> None:
        event = ResearchEventRow(
            tenant_id=task.tenant_id,
            task_id=task.task_id,
            event_type="task_updated",
            payload_json={
                "event_id": 0,
                "task_id": task.task_id,
                "changed": ["task_status"],
                "at": now.isoformat(),
                "from_status": old_status,
                "to_status": task.status,
            },
            created_at=now,
        )
        session.add(event)  # type: ignore[attr-defined]
        session.flush()  # type: ignore[attr-defined]
        event.payload_json = {**event.payload_json, "event_id": event.event_id}
        session.add(  # type: ignore[attr-defined]
            OutboxMessageRow(
                message_id=new_id("outbox"),
                tenant_id=task.tenant_id,
                kind="research.event.publish",
                aggregate_id=str(event.event_id),
                payload_json={"task_id": task.task_id, "event_id": event.event_id},
                idempotency_key=f"event:{event.event_id}",
                available_at=now,
            )
        )
