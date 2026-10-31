"""Replay failed BM25 index writes from the PostgreSQL outbox."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from core.database import SessionLocal
from models.reliability import SearchIndexOutbox

from .factory import get_knowledge_index_service


def process_search_index_outbox(*, batch_size: int = 50) -> dict[str, int]:
    service = get_knowledge_index_service()
    if service is None:
        return {"processed": 0, "succeeded": 0, "failed": 0}
    now = datetime.now(timezone.utc)
    counts = {"processed": 0, "succeeded": 0, "failed": 0}
    with SessionLocal() as db:
        rows = db.query(SearchIndexOutbox).filter(
            SearchIndexOutbox.status == "pending",
            SearchIndexOutbox.available_at <= now,
        ).order_by(SearchIndexOutbox.created_at.asc()).with_for_update(
            skip_locked=True
        ).limit(batch_size).all()
        for row in rows:
            counts["processed"] += 1
            payload = row.payload_json
            ok = service.index_chunks(
                kb_name=payload["kb_name"],
                documents=[
                    {
                        "id": item["chunk_id"],
                        "doc_id": item["document_id"],
                        "filename": item["filename"],
                        "content": item["content"],
                        "chunk_index": item["chunk_index"],
                    }
                    for item in payload["documents"]
                ],
                user_id=payload.get("user_id"),
                enqueue_on_failure=False,
            )
            row.attempts += 1
            if ok:
                row.status = "completed"
                row.last_error = None
                counts["succeeded"] += 1
            else:
                row.last_error = "index replay failed"
                row.available_at = now + timedelta(seconds=min(2 ** row.attempts, 300))
                if row.attempts >= 10:
                    row.status = "failed"
                counts["failed"] += 1
        db.commit()
    return counts
