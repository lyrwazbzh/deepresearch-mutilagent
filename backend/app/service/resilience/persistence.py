"""PostgreSQL adapters for idempotency, DLQ and indexing outbox."""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any
from sqlalchemy import text

from core.database import SessionLocal
from models.reliability import DeadLetterTask, SearchIndexOutbox, ToolExecution

from .idempotency import IdempotencyClaim


def _uuid_or_none(value: str | None):
    try:
        return uuid.UUID(value) if value else None
    except (ValueError, TypeError):
        return None


def _json_value(value: Any) -> Any:
    return json.loads(json.dumps(value, ensure_ascii=False, default=str))


class PostgresIdempotencyStore:
    def __init__(self, *, lease_seconds: int = 120):
        self.lease_seconds = lease_seconds

    def claim(self, key: str, context) -> IdempotencyClaim:
        now = datetime.now(timezone.utc)
        with SessionLocal() as db:
            db.execute(
                text("SELECT pg_advisory_xact_lock(hashtext(:lock_key))"),
                {"lock_key": f"idempotency:{key}"},
            )
            row = db.query(ToolExecution).filter(ToolExecution.idempotency_key == key).first()
            if row and row.status == "completed":
                return IdempotencyClaim(True, True, row.result_json)
            lease_active = row and row.status == "running" and row.lease_expires_at and row.lease_expires_at > now
            if lease_active:
                return IdempotencyClaim(False)
            if row is None:
                row = ToolExecution(
                    user_id=_uuid_or_none(context.user_id),
                    run_id=context.run_id,
                    session_id=context.session_id,
                    idempotency_key=key,
                    operation=context.operation_id,
                    provider=context.provider,
                )
                db.add(row)
            else:
                row.status = "running"
                row.attempts += 1
            row.lease_expires_at = now + timedelta(seconds=self.lease_seconds)
            row.updated_at = now
            db.commit()
            return IdempotencyClaim(True)

    def complete(self, key: str, result: Any) -> None:
        with SessionLocal() as db:
            row = db.query(ToolExecution).filter(ToolExecution.idempotency_key == key).one()
            row.status = "completed"
            row.result_json = _json_value(result)
            row.error_json = None
            row.lease_expires_at = None
            db.commit()

    def fail(self, key: str, error: BaseException) -> None:
        with SessionLocal() as db:
            row = db.query(ToolExecution).filter(ToolExecution.idempotency_key == key).one_or_none()
            if row:
                row.status = "failed"
                row.error_json = {
                    "error_type": type(error).__name__,
                    "message": str(error)[:1000],
                }
                row.lease_expires_at = None
                db.commit()


class PostgresDeadLetterStore:
    def enqueue(self, *, context, error: BaseException, attempts: int) -> str:
        with SessionLocal() as db:
            row = DeadLetterTask(
                user_id=_uuid_or_none(context.user_id),
                run_id=context.run_id,
                session_id=context.session_id,
                operation=context.operation_id,
                provider=context.provider,
                sanitized_payload=_json_value(context.metadata),
                error_code=type(error).__name__[:120],
                error_summary=str(error)[:2000],
                attempts=attempts,
            )
            db.add(row)
            db.commit()
            db.refresh(row)
            return str(row.id)


def enqueue_search_index_failure(operation: str, payload: dict[str, Any]) -> None:
    with SessionLocal() as db:
        db.add(SearchIndexOutbox(
            operation=operation,
            payload_json=_json_value(payload),
        ))
        db.commit()


class DeadLetterRepository:
    def list_for_user(self, user_id: str, *, status: str | None, limit: int, offset: int):
        with SessionLocal() as db:
            query = db.query(DeadLetterTask).filter(
                DeadLetterTask.user_id == _uuid_or_none(user_id)
            )
            if status:
                query = query.filter(DeadLetterTask.status == status)
            rows = query.order_by(DeadLetterTask.created_at.desc()).offset(offset).limit(limit).all()
            return [self._serialize(row) for row in rows]

    def requeue(self, task_id: str, user_id: str) -> dict[str, Any] | None:
        with SessionLocal() as db:
            row = db.query(DeadLetterTask).filter(
                DeadLetterTask.id == _uuid_or_none(task_id),
                DeadLetterTask.user_id == _uuid_or_none(user_id),
            ).first()
            if not row:
                return None
            row.status = "pending"
            row.available_at = datetime.now(timezone.utc)
            row.attempts += 1
            db.commit()
            db.refresh(row)
            return self._serialize(row)

    def resolve(self, task_id: str, user_id: str) -> dict[str, Any] | None:
        with SessionLocal() as db:
            row = db.query(DeadLetterTask).filter(
                DeadLetterTask.id == _uuid_or_none(task_id),
                DeadLetterTask.user_id == _uuid_or_none(user_id),
            ).first()
            if not row:
                return None
            row.status = "resolved"
            db.commit()
            db.refresh(row)
            return self._serialize(row)

    @staticmethod
    def _serialize(row: DeadLetterTask) -> dict[str, Any]:
        return {
            "id": str(row.id),
            "run_id": row.run_id,
            "session_id": row.session_id,
            "operation": row.operation,
            "provider": row.provider,
            "error_code": row.error_code,
            "error_summary": row.error_summary,
            "attempts": row.attempts,
            "status": row.status,
            "available_at": row.available_at,
            "created_at": row.created_at,
        }
