from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from sqlalchemy import delete, select

from src.agent.backends.persistence.database import SessionFactory
from src.agent.backends.persistence.models import AuditLogRow


AuditResult = Literal["succeeded", "denied", "failed"]


class AuditService:
    """The single append/query/retention boundary for the existing audit log."""

    def __init__(self, session_factory: SessionFactory) -> None:
        self.session_factory = session_factory

    def append(
        self,
        *,
        tenant_id: str,
        actor_ref: str,
        action: str,
        resource_ref: str,
        result: AuditResult,
        reason_code: str | None = None,
        trace_id: str | None = None,
    ) -> str:
        audit_id = f"audit_{uuid.uuid4().hex}"
        with self.session_factory() as session, session.begin():
            session.add(
                AuditLogRow(
                    audit_id=audit_id,
                    tenant_id=tenant_id,
                    actor_ref=actor_ref[:255],
                    action=action[:128],
                    resource_ref=resource_ref[:255],
                    result=result,
                    reason_code=reason_code[:128] if reason_code else None,
                    trace_id=trace_id[:64] if trace_id else None,
                )
            )
        return audit_id

    def list(self, tenant_id: str, *, limit: int = 100) -> list[AuditLogRow]:
        with self.session_factory() as session:
            return list(
                session.scalars(
                    select(AuditLogRow)
                    .where(AuditLogRow.tenant_id == tenant_id)
                    .order_by(AuditLogRow.created_at.desc(), AuditLogRow.audit_id)
                    .limit(min(max(limit, 1), 500))
                )
            )

    def purge_before(self, cutoff: datetime) -> int:
        with self.session_factory() as session, session.begin():
            result = session.execute(delete(AuditLogRow).where(AuditLogRow.created_at < cutoff))
            return int(result.rowcount or 0)

    @staticmethod
    def user_actor(user_id: str) -> str:
        return f"user:{user_id}"

    @staticmethod
    def system_actor(name: str) -> str:
        return f"system:{name}"


def current_trace_id() -> str | None:
    from opentelemetry import trace

    context = trace.get_current_span().get_span_context()
    return f"{context.trace_id:032x}" if context.is_valid else None
