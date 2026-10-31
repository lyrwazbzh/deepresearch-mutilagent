"""Durable idempotency, dead-letter and search-index outbox records."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import Column, DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB, UUID

from core.database import Base


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class ToolExecution(Base):
    __tablename__ = "tool_executions"
    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_tool_executions_idempotency_key"),
        Index("ix_tool_executions_run_status", "run_id", "status"),
    )

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=True)
    run_id = Column(String(128), nullable=False)
    session_id = Column(String(128), nullable=False)
    idempotency_key = Column(String(64), nullable=False)
    operation = Column(String(160), nullable=False)
    provider = Column(String(120), nullable=False)
    request_hash = Column(String(64), nullable=True)
    status = Column(String(24), nullable=False, default="running")
    attempts = Column(Integer, nullable=False, default=1)
    result_json = Column(JSONB, nullable=True)
    error_json = Column(JSONB, nullable=True)
    lease_expires_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=utc_now)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=utc_now, onupdate=utc_now)


class DeadLetterTask(Base):
    __tablename__ = "dead_letter_tasks"
    __table_args__ = (
        Index("ix_dead_letter_user_status", "user_id", "status"),
        Index("ix_dead_letter_available", "status", "available_at"),
    )

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=True)
    run_id = Column(String(128), nullable=False)
    session_id = Column(String(128), nullable=False)
    operation = Column(String(160), nullable=False)
    provider = Column(String(120), nullable=False)
    sanitized_payload = Column(JSONB, nullable=False, default=dict)
    error_code = Column(String(120), nullable=False)
    error_summary = Column(Text, nullable=False)
    attempts = Column(Integer, nullable=False, default=0)
    status = Column(String(24), nullable=False, default="pending")
    available_at = Column(DateTime(timezone=True), nullable=False, default=utc_now)
    created_at = Column(DateTime(timezone=True), nullable=False, default=utc_now)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=utc_now, onupdate=utc_now)


class SearchIndexOutbox(Base):
    __tablename__ = "search_index_outbox"
    __table_args__ = (
        Index("ix_search_index_outbox_status", "status", "available_at"),
    )

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    operation = Column(String(24), nullable=False)
    payload_json = Column(JSONB, nullable=False)
    status = Column(String(24), nullable=False, default="pending")
    attempts = Column(Integer, nullable=False, default=0)
    last_error = Column(Text, nullable=True)
    available_at = Column(DateTime(timezone=True), nullable=False, default=utc_now)
    created_at = Column(DateTime(timezone=True), nullable=False, default=utc_now)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=utc_now, onupdate=utc_now)
