"""Durable business-level observability records for deep research."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import (
    BigInteger,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import relationship

from core.database import Base


def utc_now() -> datetime:
    return datetime.now(UTC)


class ResearchRun(Base):
    """One attempt to execute a durable deep-research request."""

    __tablename__ = "research_runs"
    __table_args__ = (
        Index("ix_research_runs_session_started", "session_id", "started_at"),
        Index("ix_research_runs_research_started", "research_id", "started_at"),
        Index("ix_research_runs_user_status", "user_id", "status"),
    )

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    research_id = Column(UUID(as_uuid=True), nullable=False, default=uuid.uuid4, index=True)
    session_id = Column(
        UUID(as_uuid=True),
        ForeignKey("chat_sessions.id", ondelete="CASCADE"),
        nullable=False,
    )
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    request_id = Column(String(128), nullable=True)
    trace_id = Column(String(64), nullable=True, index=True)
    status = Column(String(24), nullable=False, default="running", index=True)
    current_phase = Column(String(48), nullable=True)
    query_summary = Column(JSONB, nullable=True)
    metadata_json = Column(JSONB, nullable=False, default=dict)
    started_at = Column(DateTime(timezone=True), nullable=False, default=utc_now)
    ended_at = Column(DateTime(timezone=True), nullable=True)
    duration_ms = Column(Integer, nullable=True)
    input_tokens = Column(Integer, nullable=False, default=0)
    output_tokens = Column(Integer, nullable=False, default=0)
    estimated_cost = Column(Numeric(14, 6), nullable=True)
    error_code = Column(String(80), nullable=True)
    error_summary = Column(Text, nullable=True)

    events = relationship(
        "ResearchEvent",
        back_populates="run",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="ResearchEvent.sequence",
    )

    def to_dict(self) -> dict[str, object]:
        return {
            "run_id": str(self.id),
            "research_id": str(self.research_id),
            "session_id": str(self.session_id),
            "user_id": str(self.user_id),
            "request_id": self.request_id,
            "trace_id": self.trace_id,
            "status": self.status,
            "current_phase": self.current_phase,
            "query_summary": self.query_summary,
            "metadata": self.metadata_json or {},
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "ended_at": self.ended_at.isoformat() if self.ended_at else None,
            "duration_ms": self.duration_ms,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "estimated_cost": float(self.estimated_cost) if self.estimated_cost is not None else None,
            "error_code": self.error_code,
            "error_summary": self.error_summary,
        }


class ResearchEvent(Base):
    """An append-only, sanitized event within a research run."""

    __tablename__ = "research_events"
    __table_args__ = (
        UniqueConstraint("run_id", "sequence", name="uq_research_events_run_sequence"),
        Index("ix_research_events_run_created", "run_id", "created_at"),
        Index("ix_research_events_type_created", "event_type", "created_at"),
    )

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    run_id = Column(
        UUID(as_uuid=True),
        ForeignKey("research_runs.id", ondelete="CASCADE"),
        nullable=False,
    )
    sequence = Column(Integer, nullable=False)
    event_type = Column(String(80), nullable=False)
    phase = Column(String(48), nullable=True)
    status = Column(String(24), nullable=False, default="info")
    payload = Column(JSONB, nullable=False, default=dict)
    duration_ms = Column(Integer, nullable=True)
    trace_id = Column(String(64), nullable=True)
    span_id = Column(String(32), nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=utc_now)

    run = relationship("ResearchRun", back_populates="events")

    def to_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "run_id": str(self.run_id),
            "sequence": self.sequence,
            "event_type": self.event_type,
            "phase": self.phase,
            "status": self.status,
            "payload": self.payload or {},
            "duration_ms": self.duration_ms,
            "trace_id": self.trace_id,
            "span_id": self.span_id,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }

