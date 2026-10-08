from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Protocol

from src.agent.backends.events import EventBroker
from src.agent.backends.persistence.database import SessionFactory
from src.agent.backends.persistence.repositories import OutboxRepository


class JobPublisher(Protocol):
    def publish_research(self, task_id: str) -> None: ...

    def publish_document(self, doc_id: str) -> None: ...

    def publish_database_write(self, write_id: str, plan_hash: str) -> None: ...

    def publish_memory_extraction(self, payload: dict[str, object]) -> None: ...

    def publish_memory_embedding(self, payload: dict[str, object]) -> None: ...

    def publish_memory_gc(self) -> None: ...

    def publish_knowledge_graph(self, task_id: str) -> None: ...

    def publish_export(self, export_id: str) -> None: ...


class CeleryJobPublisher:
    def __init__(self, celery_app: object) -> None:
        self.celery_app = celery_app

    def publish_research(self, task_id: str) -> None:
        self.celery_app.send_task(  # type: ignore[attr-defined]
            "research.run", args=[task_id], queue="research"
        )

    def publish_document(self, doc_id: str) -> None:
        self.celery_app.send_task(  # type: ignore[attr-defined]
            "document.ingest", args=[doc_id], queue="ingestion"
        )

    def publish_database_write(self, write_id: str, plan_hash: str) -> None:
        self.celery_app.send_task(  # type: ignore[attr-defined]
            "database_write.execute", args=[write_id, plan_hash], queue="research"
        )

    def publish_memory_extraction(self, payload: dict[str, object]) -> None:
        self.celery_app.send_task(  # type: ignore[attr-defined]
            "memory.extract", args=[payload], queue="research"
        )

    def publish_memory_embedding(self, payload: dict[str, object]) -> None:
        self.celery_app.send_task(  # type: ignore[attr-defined]
            "memory.embed", args=[payload], queue="research"
        )

    def publish_memory_gc(self) -> None:
        self.celery_app.send_task(  # type: ignore[attr-defined]
            "memory.gc", queue="research"
        )

    def publish_knowledge_graph(self, task_id: str) -> None:
        self.celery_app.send_task(  # type: ignore[attr-defined]
            "knowledge_graph.build", args=[task_id], queue="research"
        )

    def publish_export(self, export_id: str) -> None:
        self.celery_app.send_task(  # type: ignore[attr-defined]
            "report.export", args=[export_id], queue="export"
        )


class OutboxDispatcher:
    def __init__(
        self,
        session_factory: SessionFactory,
        job_publisher: JobPublisher,
        event_broker: EventBroker,
    ) -> None:
        self.session_factory = session_factory
        self.job_publisher = job_publisher
        self.event_broker = event_broker
        self._event_loop = asyncio.new_event_loop()

    def dispatch_once(self, limit: int = 100) -> int:
        count = 0
        with self.session_factory() as session, session.begin():
            now = datetime.now(timezone.utc)
            rows = OutboxRepository(session).pending(now, limit)
            latest_extraction: dict[tuple[str, str], object] = {}
            for pending in rows:
                if pending.kind == "memory.extraction.requested":
                    latest_extraction[(pending.tenant_id, pending.aggregate_id)] = pending
            for row in rows:
                if (
                    row.kind == "memory.extraction.requested"
                    and latest_extraction[(row.tenant_id, row.aggregate_id)] is not row
                ):
                    row.dispatched_at = now
                    count += 1
                    continue
                if row.kind == "research.schedule":
                    self.job_publisher.publish_research(str(row.payload_json["task_id"]))
                elif row.kind == "document.ingest":
                    self.job_publisher.publish_document(str(row.payload_json["doc_id"]))
                elif row.kind == "database_write.execute":
                    self.job_publisher.publish_database_write(
                        str(row.payload_json["write_id"]),
                        str(row.payload_json["plan_hash"]),
                    )
                elif row.kind == "memory.extraction.requested":
                    self.job_publisher.publish_memory_extraction(dict(row.payload_json))
                elif row.kind == "memory.embedding.requested":
                    self.job_publisher.publish_memory_embedding(dict(row.payload_json))
                elif row.kind == "memory.gc":
                    self.job_publisher.publish_memory_gc()
                elif row.kind == "knowledge_graph.build":
                    self.job_publisher.publish_knowledge_graph(
                        str(row.payload_json["task_id"])
                    )
                elif row.kind == "report.export":
                    self.job_publisher.publish_export(
                        str(row.payload_json["export_id"])
                    )
                elif row.kind == "research.event.publish":
                    self._event_loop.run_until_complete(
                        self.event_broker.publish(
                            row.tenant_id,
                            str(row.payload_json["task_id"]),
                            int(row.payload_json["event_id"]),
                        )
                    )
                else:
                    continue
                row.dispatched_at = now
                count += 1
        return count
