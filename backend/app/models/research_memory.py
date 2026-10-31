"""Versioned fact and experience memories for research planning."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import Column, DateTime, Float, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB, UUID

from core.database import Base


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class FactMemory(Base):
    __tablename__ = "fact_memories"
    __table_args__ = (
        UniqueConstraint("user_id", "canonical_key", "version", name="uq_fact_memory_version"),
        Index("ix_fact_memory_user_status", "user_id", "status"),
        Index("ix_fact_memory_user_tier", "user_id", "storage_tier", "status"),
        Index("ix_fact_memory_canonical", "user_id", "canonical_key"),
    )

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    canonical_key = Column(String(64), nullable=False)
    content_hash = Column(String(64), nullable=False)
    subject = Column(String(500), nullable=False)
    predicate = Column(String(255), nullable=False)
    value = Column(Text, nullable=False)
    content = Column(Text, nullable=False)
    source_url = Column(Text, nullable=True)
    source_name = Column(String(500), nullable=True)
    source_type = Column(String(80), nullable=True)
    confidence = Column(Float, nullable=False, default=0.5)
    version = Column(Integer, nullable=False, default=1)
    parent_id = Column(UUID(as_uuid=True), ForeignKey("fact_memories.id", ondelete="SET NULL"), nullable=True)
    status = Column(String(24), nullable=False, default="active")
    storage_tier = Column(String(16), nullable=False, default="hot")
    observed_at = Column(DateTime(timezone=True), nullable=True)
    last_accessed_at = Column(DateTime(timezone=True), nullable=True)
    access_count = Column(Integer, nullable=False, default=0)
    cold_at = Column(DateTime(timezone=True), nullable=True)
    rehydrated_at = Column(DateTime(timezone=True), nullable=True)
    metadata_json = Column(JSONB, nullable=False, default=dict)
    created_at = Column(DateTime(timezone=True), nullable=False, default=utc_now)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=utc_now, onupdate=utc_now)


class ExperienceMemory(Base):
    __tablename__ = "experience_memories"
    __table_args__ = (
        Index("ix_experience_memory_user_status", "user_id", "status"),
        Index("ix_experience_memory_user_tier", "user_id", "storage_tier", "status"),
        Index("ix_experience_memory_run", "run_id"),
    )

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    run_id = Column(String(128), nullable=False)
    scenario = Column(Text, nullable=False)
    trigger_pattern = Column(Text, nullable=False)
    strategy = Column(Text, nullable=False)
    outcome = Column(Text, nullable=False)
    critic_summary = Column(Text, nullable=True)
    initial_score = Column(Float, nullable=True)
    final_score = Column(Float, nullable=True)
    status = Column(String(24), nullable=False, default="active")
    storage_tier = Column(String(16), nullable=False, default="hot")
    last_accessed_at = Column(DateTime(timezone=True), nullable=True)
    access_count = Column(Integer, nullable=False, default=0)
    cold_at = Column(DateTime(timezone=True), nullable=True)
    rehydrated_at = Column(DateTime(timezone=True), nullable=True)
    metadata_json = Column(JSONB, nullable=False, default=dict)
    created_at = Column(DateTime(timezone=True), nullable=False, default=utc_now)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=utc_now, onupdate=utc_now)
