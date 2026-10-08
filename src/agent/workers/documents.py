from __future__ import annotations

from datetime import timedelta

from sqlalchemy import select

from src.agent.tools.documents import DocumentService, IngestionService, VectorRuntime
from src.agent.llm.runtime import ModelResolver
from src.agent.backends.persistence.database import SessionFactory
from src.agent.backends.persistence.models import DocumentIngestionRow, DocumentRow, utcnow


class DocumentIngestionWorker:
    """At-least-once document job handler with generation/attempt fencing."""

    def __init__(
        self,
        session_factory: SessionFactory,
        documents: DocumentService,
        model_runtime: VectorRuntime,
        model_resolver: ModelResolver,
        *,
        stale_after: timedelta = timedelta(minutes=15),
    ) -> None:
        self.session_factory = session_factory
        self.documents = documents
        self.model_runtime = model_runtime
        self.model_resolver = model_resolver
        self.stale_after = stale_after

    def run(self, doc_id: str) -> int | None:
        now = utcnow()
        with self.session_factory() as session, session.begin():
            document = session.scalar(
                select(DocumentRow).where(DocumentRow.doc_id == doc_id).with_for_update()
            )
            ingestion = session.get(DocumentIngestionRow, doc_id)
            if document is None or ingestion is None or document.deleted_at is not None:
                return None
            if ingestion.status == "succeeded":
                return ingestion.generation
            if ingestion.status == "processing":
                updated_at = ingestion.updated_at
                if updated_at.tzinfo is None:
                    updated_at = updated_at.replace(tzinfo=now.tzinfo)
                if updated_at > now - self.stale_after:
                    return None
                ingestion.status = "failed"
                ingestion.error_code = "worker_lost"
                ingestion.updated_at = now
            tenant_id = document.tenant_id

        with self.session_factory() as session:
            binding = self.model_resolver.resolve_snapshot(
                session, tenant_id, ("embedding",)
            ).get("embedding")
        return IngestionService(
            self.session_factory,
            self.documents,
            self.model_runtime,
            binding,
        ).run(tenant_id, doc_id)
