from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    DDL,
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    LargeBinary,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    event,
    text,
)
from sqlalchemy.types import UserDefinedType
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    type_annotation_map = {dict[str, Any]: JSON, list[str]: JSON}


class PGVector(UserDefinedType[list[float]]):
    """Small pgvector SQL type without adding a second vector client dependency."""

    cache_ok = True

    def __init__(self, dimension: int) -> None:
        self.dimension = dimension

    def get_col_spec(self, **_kwargs: Any) -> str:
        return f"VECTOR({self.dimension})"

    def bind_processor(self, _dialect: Any):
        def process(value: list[float] | None) -> str | None:
            if value is None:
                return None
            return "[" + ",".join(format(float(item), ".12g") for item in value) + "]"

        return process

    def result_processor(self, _dialect: Any, _coltype: Any):
        def process(value: Any) -> list[float] | None:
            if value is None or isinstance(value, list):
                return value
            rendered = str(value).strip("[]")
            return [] if not rendered else [float(item) for item in rendered.split(",")]

        return process


event.listen(
    Base.metadata,
    "before_create",
    DDL("CREATE EXTENSION IF NOT EXISTS vector").execute_if(dialect="postgresql"),
)


class TenantRow(Base):
    __tablename__ = "tenants"
    tenant_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class UserRow(Base):
    __tablename__ = "users"
    user_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    email: Mapped[str] = mapped_column(String(320), nullable=False)
    email_normalized: Mapped[str] = mapped_column(String(320), nullable=False, unique=True)
    password_hash: Mapped[str] = mapped_column(Text, nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class TenantUserMembershipRow(Base):
    __tablename__ = "tenant_user_memberships"
    tenant_id: Mapped[str] = mapped_column(
        ForeignKey("tenants.tenant_id"), primary_key=True
    )
    user_id: Mapped[str] = mapped_column(ForeignKey("users.user_id"), primary_key=True)
    role: Mapped[str] = mapped_column(String(16), nullable=False, default="member")
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    __table_args__ = (
        CheckConstraint("role IN ('member','admin')", name="ck_membership_role"),
        Index("ix_membership_user_active", "user_id", "active"),
    )


class AuthSessionRow(Base):
    __tablename__ = "auth_sessions"
    session_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.tenant_id"), nullable=False)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.user_id"), nullable=False)
    refresh_secret_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    __table_args__ = (
        Index("ix_auth_sessions_owner", "tenant_id", "user_id", "revoked_at"),
        CheckConstraint("length(refresh_secret_hash) = 64", name="ck_refresh_secret_hash"),
    )


class ThreadRow(Base):
    __tablename__ = "threads"
    thread_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    user_id: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    __table_args__ = (Index("ix_threads_scope", "tenant_id", "user_id", "updated_at"),)


class ConversationMessageRow(Base):
    __tablename__ = "conversation_messages"
    message_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    user_id: Mapped[str] = mapped_column(String(64), nullable=False)
    thread_id: Mapped[str] = mapped_column(ForeignKey("threads.thread_id"), nullable=False)
    task_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    __table_args__ = (
        CheckConstraint("role IN ('user','assistant')", name="ck_conversation_message_role"),
        Index("ix_messages_thread", "tenant_id", "user_id", "thread_id", "created_at"),
    )


class ResearchTaskRow(Base):
    __tablename__ = "research_tasks"
    task_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    user_id: Mapped[str] = mapped_column(String(64), nullable=False)
    thread_id: Mapped[str] = mapped_column(ForeignKey("threads.thread_id"), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    latest_state: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    model_binding_snapshot: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    capability_snapshot: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    create_idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False)
    create_request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    lease_token: Mapped[str | None] = mapped_column(String(64))
    leased_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    task_budget: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    budget_accounted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "user_id", "create_idempotency_key", name="uq_task_create_key"
        ),
        CheckConstraint(
            "status IN ('pending','running','pausing','paused','cancelling','cancelled','completed','failed')",
            name="ck_research_task_status",
        ),
        Index("ix_tasks_scope_status", "tenant_id", "user_id", "status"),
    )


class ResearchTaskCommandRow(Base):
    __tablename__ = "research_task_commands"
    tenant_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    task_id: Mapped[str] = mapped_column(
        ForeignKey("research_tasks.task_id"), primary_key=True
    )
    idempotency_key: Mapped[str] = mapped_column(String(255), primary_key=True)
    command_type: Mapped[str] = mapped_column(String(16), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    accepted_http_status: Mapped[int] = mapped_column(Integer, nullable=False)
    response_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    __table_args__ = (
        CheckConstraint(
            "command_type IN ('pause','resume','cancel','replan','approve','reject')",
            name="ck_research_task_command_type",
        ),
        Index("ix_task_commands_task", "tenant_id", "task_id", "created_at"),
    )


class ResearchCheckpointRow(Base):
    __tablename__ = "research_checkpoints"
    checkpoint_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    task_id: Mapped[str] = mapped_column(ForeignKey("research_tasks.task_id"), nullable=False)
    checkpoint_seq: Mapped[int] = mapped_column(Integer, nullable=False)
    state_snapshot: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    graph_position: Mapped[str] = mapped_column(String(64), nullable=False)
    scheduling_state: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    __table_args__ = (
        UniqueConstraint("tenant_id", "task_id", "checkpoint_seq", name="uq_checkpoint_seq"),
        Index("ix_checkpoints_task", "tenant_id", "task_id", "checkpoint_seq"),
    )


class NodeRunRow(Base):
    __tablename__ = "node_runs"
    node_run_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    task_id: Mapped[str] = mapped_column(ForeignKey("research_tasks.task_id"), nullable=False)
    node_key: Mapped[str] = mapped_column(String(128), nullable=False)
    node_name: Mapped[str] = mapped_column(String(64), nullable=False)
    input_checkpoint_id: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    result_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    output_checkpoint_id: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    __table_args__ = (
        UniqueConstraint("task_id", "node_key", "input_checkpoint_id", name="uq_node_semantic"),
        CheckConstraint("status IN ('pending','running','completed','failed')", name="ck_node_status"),
        Index("ix_node_runs_task", "tenant_id", "task_id", "status"),
    )


class NodeEffectRow(Base):
    __tablename__ = "node_effects"
    effect_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    task_id: Mapped[str] = mapped_column(String(64), nullable=False)
    node_run_id: Mapped[str] = mapped_column(ForeignKey("node_runs.node_run_id"), nullable=False)
    effect_name: Mapped[str] = mapped_column(String(128), nullable=False)
    operation_key: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    result_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "task_id", "node_run_id", "effect_name", name="uq_node_effect"
        ),
    )


class ResearchEventRow(Base):
    __tablename__ = "research_events"
    event_id: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    task_id: Mapped[str] = mapped_column(ForeignKey("research_tasks.task_id"), nullable=False)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    __table_args__ = (Index("ix_events_task_cursor", "tenant_id", "task_id", "event_id"),)


class OutboxMessageRow(Base):
    __tablename__ = "outbox_messages"
    message_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    kind: Mapped[str] = mapped_column(String(64), nullable=False)
    aggregate_id: Mapped[str] = mapped_column(String(64), nullable=False)
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False)
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    dispatched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    __table_args__ = (
        UniqueConstraint("tenant_id", "kind", "idempotency_key", name="uq_outbox_idempotency"),
        Index("ix_outbox_pending", "dispatched_at", "available_at"),
    )


class TaskKnowledgeGraphRow(Base):
    __tablename__ = "task_knowledge_graphs"
    tenant_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    task_id: Mapped[str] = mapped_column(
        ForeignKey("research_tasks.task_id"), primary_key=True
    )
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="building")
    query_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    plan_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    processed_evidence_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    __table_args__ = (
        CheckConstraint(
            "status IN ('building','ready','partial')",
            name="ck_task_knowledge_graph_status",
        ),
        CheckConstraint(
            "processed_evidence_count >= 0",
            name="ck_task_knowledge_graph_processed_count",
        ),
        Index("ix_task_knowledge_graph_status", "tenant_id", "status", "updated_at"),
    )


class ModelConfigRow(Base):
    __tablename__ = "model_configs"
    model_config_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    version: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    user_id: Mapped[str] = mapped_column(String(64), nullable=False)
    provider: Mapped[str] = mapped_column(String(64), nullable=False)
    model: Mapped[str] = mapped_column(String(255), nullable=False)
    base_url: Mapped[str | None] = mapped_column(String(1024))
    purpose: Mapped[str] = mapped_column(String(16), nullable=False)
    secret_ref: Mapped[str | None] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    __table_args__ = (
        UniqueConstraint("tenant_id", "model_config_id", "version", name="uq_model_config_tenant"),
        UniqueConstraint(
            "tenant_id", "user_id", "model_config_id", "version", name="uq_model_config_owner"
        ),
        CheckConstraint("purpose IN ('chat','embedding','rerank')", name="ck_model_purpose"),
    )


class ModelPricingRow(Base):
    __tablename__ = "model_pricing"
    tenant_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    provider: Mapped[str] = mapped_column(String(64), primary_key=True)
    model: Mapped[str] = mapped_column(String(255), primary_key=True)
    currency: Mapped[str] = mapped_column(String(8), primary_key=True)
    effective_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), primary_key=True
    )
    input_price_per_1m_tokens: Mapped[Decimal] = mapped_column(
        Numeric(20, 8), nullable=False
    )
    output_price_per_1m_tokens: Mapped[Decimal] = mapped_column(
        Numeric(20, 8), nullable=False
    )
    __table_args__ = (
        CheckConstraint(
            "input_price_per_1m_tokens >= 0",
            name="ck_model_pricing_input_nonnegative",
        ),
        CheckConstraint(
            "output_price_per_1m_tokens >= 0",
            name="ck_model_pricing_output_nonnegative",
        ),
        Index(
            "ix_model_pricing_lookup",
            "tenant_id",
            "provider",
            "model",
            "currency",
            "effective_at",
        ),
    )


class ModelConfigHeadRow(Base):
    __tablename__ = "model_config_heads"
    tenant_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    model_config_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(64), nullable=False)
    current_version: Mapped[int] = mapped_column(Integer, nullable=False)
    enabled_for_new_tasks: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "user_id", "model_config_id", "current_version"],
            [
                "model_configs.tenant_id",
                "model_configs.user_id",
                "model_configs.model_config_id",
                "model_configs.version",
            ],
            name="fk_model_head_revision",
        ),
    )


class ModelBindingRow(Base):
    __tablename__ = "model_bindings"
    tenant_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    binding_key: Mapped[str] = mapped_column(String(32), primary_key=True)
    purpose: Mapped[str] = mapped_column(String(16), primary_key=True)
    priority: Mapped[int] = mapped_column(Integer, primary_key=True)
    model_config_id: Mapped[str] = mapped_column(String(64), nullable=False)
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "model_config_id"],
            ["model_config_heads.tenant_id", "model_config_heads.model_config_id"],
            name="fk_model_binding_head",
        ),
    )


class ModelConfigRevocationRow(Base):
    __tablename__ = "model_config_revocations"
    model_config_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    version: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    user_id: Mapped[str] = mapped_column(String(64), nullable=False)
    revoked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "user_id", "model_config_id", "version"],
            [
                "model_configs.tenant_id",
                "model_configs.user_id",
                "model_configs.model_config_id",
                "model_configs.version",
            ],
            name="fk_model_revocation_revision",
        ),
    )


class ResearchMemoryRow(Base):
    __tablename__ = "research_memories"
    memory_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    user_id: Mapped[str] = mapped_column(String(64), nullable=False)
    topic: Mapped[str] = mapped_column(String(255), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    supporting_message_ids: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    confidence: Mapped[float] = mapped_column(Numeric(3, 2), nullable=False)
    embedding: Mapped[list[float] | None] = mapped_column(
        JSON(none_as_null=True).with_variant(PGVector(1024), "postgresql"), nullable=True
    )
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    write_key: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    related_memory_id: Mapped[str | None] = mapped_column(String(64))
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    __table_args__ = (
        UniqueConstraint("tenant_id", "user_id", "write_key", name="uq_memory_write_key"),
        CheckConstraint(
            "source IN ('explicit_remember','turn_completion','explicit_correction','manual_edit')",
            name="ck_memory_source",
        ),
        CheckConstraint(
            "status IN ('active','pending_conflict','superseded','forgotten')",
            name="ck_memory_status",
        ),
        CheckConstraint("confidence >= 0 AND confidence <= 1", name="ck_memory_confidence"),
        CheckConstraint("revision >= 1", name="ck_memory_revision"),
        CheckConstraint("length(content_hash) = 64", name="ck_memory_content_hash"),
        CheckConstraint("length(write_key) = 64", name="ck_memory_write_key"),
        Index("ix_memories_scope_status_topic", "tenant_id", "user_id", "status", "topic"),
        Index(
            "uq_memory_topic_content_live",
            "tenant_id",
            "user_id",
            "topic",
            "content_hash",
            unique=True,
            sqlite_where=text("status IN ('active','pending_conflict')"),
            postgresql_where=text("status IN ('active','pending_conflict')"),
        ),
        Index(
            "uq_memory_active_topic",
            "tenant_id",
            "user_id",
            "topic",
            unique=True,
            sqlite_where=text("status = 'active'"),
            postgresql_where=text("status = 'active'"),
        ),
        Index(
            "ix_memories_embedding_hnsw",
            "embedding",
            postgresql_using="hnsw",
            postgresql_ops={"embedding": "vector_cosine_ops"},
            postgresql_where=text("embedding IS NOT NULL"),
            sqlite_where=text("embedding IS NOT NULL"),
        ),
    )


class WorkspaceFileRow(Base):
    __tablename__ = "workspace_files"
    workspace_file_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    task_id: Mapped[str] = mapped_column(ForeignKey("research_tasks.task_id"), nullable=False)
    workspace_path: Mapped[str] = mapped_column(String(1024), nullable=False)
    storage_ref: Mapped[str | None] = mapped_column(String(2048))
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    mime_type: Mapped[str] = mapped_column(String(255), nullable=False)
    size: Mapped[int] = mapped_column(BigInteger, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    operation_key: Mapped[str] = mapped_column(String(64), nullable=False)
    producer_tool_call_id: Mapped[str | None] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    __table_args__ = (
        UniqueConstraint("tenant_id", "task_id", "workspace_path", name="uq_workspace_path"),
        UniqueConstraint("tenant_id", "task_id", "operation_key", name="uq_workspace_operation"),
        CheckConstraint("status IN ('pending','ready','failed')", name="ck_workspace_status"),
        CheckConstraint("size >= 0", name="ck_workspace_size"),
        Index("ix_workspace_task_status", "tenant_id", "task_id", "status"),
        Index("ix_workspace_producer", "tenant_id", "task_id", "producer_tool_call_id"),
    )


class SecretRow(Base):
    """Authenticated ciphertext; plaintext credentials never enter PostgreSQL."""

    __tablename__ = "secrets"
    tenant_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    secret_ref: Mapped[str] = mapped_column(String(255), primary_key=True)
    revision: Mapped[int] = mapped_column(Integer, primary_key=True)
    key_id: Mapped[str] = mapped_column(String(128), nullable=False)
    nonce: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    ciphertext: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (
        CheckConstraint("revision >= 1", name="ck_secret_revision"),
        CheckConstraint("length(nonce) = 12", name="ck_secret_nonce_length"),
        Index(
            "uq_secret_active_revision",
            "tenant_id",
            "secret_ref",
            unique=True,
            sqlite_where=text("revoked_at IS NULL"),
            postgresql_where=text("revoked_at IS NULL"),
        ),
        Index(
            "ix_secrets_tenant_ref_revision",
            "tenant_id",
            "secret_ref",
            "revision",
        ),
    )


class ShareRow(Base):
    __tablename__ = "shares"
    share_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    task_id: Mapped[str] = mapped_column(ForeignKey("research_tasks.task_id"), nullable=False)
    report_version: Mapped[int] = mapped_column(Integer, nullable=False)
    snapshot_workspace_path: Mapped[str] = mapped_column(String(1024), nullable=False)
    public_token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    title: Mapped[str] = mapped_column(String(512), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (
        CheckConstraint("report_version >= 1", name="ck_share_report_version"),
        CheckConstraint("length(public_token_hash) = 64", name="ck_share_token_hash"),
        Index("ix_shares_owner_created", "tenant_id", "task_id", "created_at"),
        Index("ix_shares_active", "tenant_id", "revoked_at", "expires_at"),
    )


class ExportJobRow(Base):
    __tablename__ = "export_jobs"
    export_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    task_id: Mapped[str] = mapped_column(ForeignKey("research_tasks.task_id"), nullable=False)
    report_version: Mapped[int] = mapped_column(Integer, nullable=False)
    format: Mapped[str] = mapped_column(String(8), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    workspace_path: Mapped[str | None] = mapped_column(String(1024))
    idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error_code: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "task_id", "idempotency_key", name="uq_export_idempotency"
        ),
        CheckConstraint("report_version >= 1", name="ck_export_report_version"),
        CheckConstraint("format IN ('docx','pdf')", name="ck_export_format"),
        CheckConstraint(
            "status IN ('pending','running','completed','failed')",
            name="ck_export_status",
        ),
        CheckConstraint("attempt_count >= 0", name="ck_export_attempt_count"),
        CheckConstraint("length(request_hash) = 64", name="ck_export_request_hash"),
        Index("ix_exports_owner_created", "tenant_id", "task_id", "created_at"),
        Index("ix_exports_status_updated", "status", "updated_at"),
    )


class DocumentRow(Base):
    __tablename__ = "documents"
    doc_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(String(512), nullable=False)
    media_type: Mapped[str] = mapped_column(String(255), nullable=False)
    tags: Mapped[list[str] | None] = mapped_column(JSON)
    storage_ref: Mapped[str] = mapped_column(String(2048), nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    published_generation: Mapped[int | None] = mapped_column(Integer)
    indexed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    __table_args__ = (
        CheckConstraint("status IN ('pending','ready','failed')", name="ck_document_status"),
        Index("ix_documents_tenant_status", "tenant_id", "status", "updated_at"),
    )


class DocumentIngestionRow(Base):
    __tablename__ = "document_ingestions"
    doc_id: Mapped[str] = mapped_column(ForeignKey("documents.doc_id"), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    generation: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="queued")
    stage: Mapped[str] = mapped_column(String(32), nullable=False, default="parsing")
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error_code: Mapped[str | None] = mapped_column(String(64))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    __table_args__ = (
        CheckConstraint(
            "status IN ('queued','processing','succeeded','failed')",
            name="ck_document_ingestion_status",
        ),
        CheckConstraint(
            "stage IN ('parsing','chunking','embedding','indexing','publishing')",
            name="ck_document_ingestion_stage",
        ),
        Index("ix_ingestions_tenant_status", "tenant_id", "status", "updated_at"),
    )


class DocumentChunkRow(Base):
    __tablename__ = "document_chunks"
    chunk_id: Mapped[str] = mapped_column(String(96), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    doc_id: Mapped[str] = mapped_column(ForeignKey("documents.doc_id"), nullable=False)
    generation: Mapped[int] = mapped_column(Integer, nullable=False)
    parent_id: Mapped[str | None] = mapped_column(String(96))
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    section_title: Mapped[str | None] = mapped_column(String(512))
    page_start: Mapped[int | None] = mapped_column(Integer)
    page_end: Mapped[int | None] = mapped_column(Integer)
    chunk_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    metadata_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    __table_args__ = (
        Index("ix_chunks_document_generation", "tenant_id", "doc_id", "generation"),
    )


class DocumentUploadReservationRow(Base):
    __tablename__ = "document_upload_reservations"
    tenant_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    idempotency_key: Mapped[str] = mapped_column(String(255), primary_key=True)
    doc_id: Mapped[str] = mapped_column(String(64), nullable=False)
    content_hash: Mapped[str | None] = mapped_column(String(64))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class DataSourceRow(Base):
    __tablename__ = "datasources"
    datasource_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    host: Mapped[str] = mapped_column(String(512), nullable=False)
    port: Mapped[int] = mapped_column(Integer, nullable=False, default=5432)
    database_name: Mapped[str] = mapped_column(String(255), nullable=False)
    ssl_mode: Mapped[str] = mapped_column(String(32), nullable=False, default="require")
    credential_ref: Mapped[str] = mapped_column(String(255), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    __table_args__ = (
        UniqueConstraint("tenant_id", "name", name="uq_datasource_name"),
        Index("ix_datasource_tenant_enabled", "tenant_id", "enabled"),
    )


class DataSourceUserGrantRow(Base):
    __tablename__ = "datasource_user_grants"
    tenant_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    datasource_id: Mapped[str] = mapped_column(
        ForeignKey("datasources.datasource_id"), primary_key=True
    )
    user_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    access_level: Mapped[str] = mapped_column(String(16), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    __table_args__ = (
        CheckConstraint("access_level IN ('read','write')", name="ck_datasource_access"),
    )


class DataSourceTablePolicyRow(Base):
    __tablename__ = "datasource_table_policies"
    tenant_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    datasource_id: Mapped[str] = mapped_column(
        ForeignKey("datasources.datasource_id"), primary_key=True
    )
    schema_name: Mapped[str] = mapped_column(String(128), primary_key=True)
    table_name: Mapped[str] = mapped_column(String(128), primary_key=True)
    readable_columns: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    writable_columns: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    allow_insert: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    allow_update: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    allow_delete: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    max_write_rows: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class PendingWriteRow(Base):
    __tablename__ = "pending_writes"
    tenant_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    write_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    task_id: Mapped[str] = mapped_column(ForeignKey("research_tasks.task_id"), nullable=False)
    user_id: Mapped[str] = mapped_column(String(64), nullable=False)
    plan_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    plan_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    preview_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    policy_revision: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    decision_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    operation_key: Mapped[str] = mapped_column(String(64), nullable=False)
    result_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    __table_args__ = (
        UniqueConstraint("tenant_id", "operation_key", name="uq_pending_write_operation"),
        CheckConstraint(
            "status IN ('pending','approved','executing','rejected','executed','failed','expired','stale')",
            name="ck_pending_write_status",
        ),
        CheckConstraint("length(plan_hash) = 64", name="ck_pending_write_plan_hash"),
        Index("ix_pending_write_task_status", "tenant_id", "task_id", "status", "created_at"),
        Index("ix_pending_write_owner", "tenant_id", "user_id", "task_id"),
    )


class AuditLogRow(Base):
    __tablename__ = "audit_logs"
    audit_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    actor_ref: Mapped[str] = mapped_column(String(255), nullable=False)
    action: Mapped[str] = mapped_column(String(128), nullable=False)
    resource_ref: Mapped[str] = mapped_column(String(255), nullable=False)
    result: Mapped[str] = mapped_column(String(16), nullable=False)
    reason_code: Mapped[str | None] = mapped_column(String(128))
    trace_id: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    __table_args__ = (
        CheckConstraint("result IN ('succeeded','denied','failed')", name="ck_audit_result"),
        Index("ix_audit_tenant_created", "tenant_id", "created_at"),
        Index("ix_audit_resource_created", "tenant_id", "resource_ref", "created_at"),
    )


class ObservabilitySpanRow(Base):
    """Bounded OpenTelemetry projection; never a source of business state."""

    __tablename__ = "observability_spans"
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    user_id: Mapped[str | None] = mapped_column(String(64))
    task_id: Mapped[str | None] = mapped_column(String(64))
    trace_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    span_id: Mapped[str] = mapped_column(String(16), primary_key=True)
    parent_span_id: Mapped[str | None] = mapped_column(String(16))
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ended_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    status_code: Mapped[str] = mapped_column(String(16), nullable=False)
    attributes: Mapped[dict[str, Any]] = mapped_column(
        JSON().with_variant(JSONB, "postgresql"), nullable=False, default=dict
    )
    events: Mapped[list[dict[str, Any]]] = mapped_column(
        JSON().with_variant(JSONB, "postgresql"), nullable=False, default=list
    )
    __table_args__ = (
        CheckConstraint(
            "kind IN ('INTERNAL','CLIENT','PRODUCER','CONSUMER','SERVER')",
            name="ck_observability_span_kind",
        ),
        Index(
            "ix_observability_task_started",
            "tenant_id",
            "task_id",
            text("started_at DESC"),
            sqlite_where=text("task_id IS NOT NULL"),
            postgresql_where=text("task_id IS NOT NULL"),
        ),
        Index("ix_observability_trace", "tenant_id", "trace_id"),
        Index("ix_observability_started", "started_at"),
    )


class EvalRunRow(Base):
    __tablename__ = "eval_runs"
    eval_run_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    suite: Mapped[str] = mapped_column(String(32), nullable=False)
    dataset_ref: Mapped[str] = mapped_column(String(96), nullable=False)
    config_snapshot: Mapped[dict[str, Any]] = mapped_column(
        JSON().with_variant(JSONB, "postgresql"), nullable=False
    )
    code_revision: Mapped[str] = mapped_column(String(128), nullable=False)
    eval_scope_id: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="running")
    metrics_summary: Mapped[dict[str, Any] | None] = mapped_column(
        JSON().with_variant(JSONB, "postgresql")
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (
        CheckConstraint(
            "suite IN ('rag','research_ltm','e2e','skill')", name="ck_eval_run_suite"
        ),
        CheckConstraint(
            "status IN ('running','completed','failed')", name="ck_eval_run_status"
        ),
        Index("ix_eval_run_suite_created", "suite", "created_at"),
        Index("ix_eval_run_dataset_config", "dataset_ref", "code_revision"),
    )


class EvalCaseResultRow(Base):
    __tablename__ = "eval_case_results"
    eval_run_id: Mapped[str] = mapped_column(
        ForeignKey("eval_runs.eval_run_id", ondelete="CASCADE"), primary_key=True
    )
    case_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    outcome: Mapped[str] = mapped_column(String(32), nullable=False)
    metrics: Mapped[dict[str, Any]] = mapped_column(
        JSON().with_variant(JSONB, "postgresql"), nullable=False, default=dict
    )
    diagnostics: Mapped[dict[str, Any]] = mapped_column(
        JSON().with_variant(JSONB, "postgresql"), nullable=False, default=dict
    )
    __table_args__ = (Index("ix_eval_case_outcome", "eval_run_id", "outcome"),)


class DecisionTraceRow(Base):
    """Training candidate captured beside, never inside, business state."""

    __tablename__ = "decision_traces"
    decision_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    user_id: Mapped[str] = mapped_column(String(64), nullable=False)
    task_id: Mapped[str] = mapped_column(String(64), nullable=False)
    thread_id: Mapped[str] = mapped_column(String(64), nullable=False)
    node_run_id: Mapped[str] = mapped_column(String(64), nullable=False)
    decision_index: Mapped[int] = mapped_column(Integer, nullable=False)
    agent_name: Mapped[str] = mapped_column(String(32), nullable=False)
    model_binding: Mapped[dict[str, Any]] = mapped_column(
        JSON().with_variant(JSONB, "postgresql"), nullable=False
    )
    context: Mapped[dict[str, Any]] = mapped_column(
        JSON().with_variant(JSONB, "postgresql"), nullable=False
    )
    decision: Mapped[dict[str, Any]] = mapped_column(
        JSON().with_variant(JSONB, "postgresql"), nullable=False
    )
    execution: Mapped[dict[str, Any] | None] = mapped_column(
        JSON().with_variant(JSONB, "postgresql")
    )
    outcome: Mapped[dict[str, Any] | None] = mapped_column(
        JSON().with_variant(JSONB, "postgresql")
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "task_id",
            "node_run_id",
            "decision_index",
            name="uq_decision_trace_position",
        ),
        CheckConstraint(
            "agent_name IN ('deep_scout','data_analyst','code_wizard')",
            name="ck_decision_trace_agent",
        ),
        CheckConstraint("decision_index >= 1", name="ck_decision_trace_index"),
        Index("ix_decision_trace_scope_created", "tenant_id", "user_id", "created_at"),
        Index("ix_decision_trace_task_node", "tenant_id", "task_id", "node_run_id"),
    )


class ToolDecisionReviewRow(Base):
    """Append-only human/curated label revisions for one Tool decision."""

    __tablename__ = "tool_decision_reviews"
    review_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    decision_id: Mapped[str] = mapped_column(
        ForeignKey("decision_traces.decision_id", ondelete="CASCADE"), nullable=False
    )
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    disposition: Mapped[str] = mapped_column(String(16), nullable=False)
    label: Mapped[dict[str, Any] | None] = mapped_column(
        JSON().with_variant(JSONB, "postgresql")
    )
    reviewer_ref: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    __table_args__ = (
        UniqueConstraint("decision_id", "revision", name="uq_tool_review_revision"),
        CheckConstraint(
            "disposition IN ('accepted','excluded')", name="ck_tool_review_disposition"
        ),
        CheckConstraint("revision >= 1", name="ck_tool_review_revision"),
        Index("ix_tool_review_latest", "decision_id", "revision"),
    )


class DatasetCatalogRow(Base):
    """Internal SQL/Data manifest; Workspace remains the byte-level source of truth."""

    __tablename__ = "dataset_catalog"
    dataset_ref: Mapped[str] = mapped_column(String(96), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    task_id: Mapped[str] = mapped_column(ForeignKey("research_tasks.task_id"), nullable=False)
    source_workspace_path: Mapped[str] = mapped_column(String(1024), nullable=False)
    source_content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    manifest_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    operation_key: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    __table_args__ = (
        UniqueConstraint("tenant_id", "task_id", "operation_key", name="uq_dataset_operation"),
        CheckConstraint("status IN ('pending','ready','failed')", name="ck_dataset_status"),
        Index("ix_dataset_task", "tenant_id", "task_id", "status"),
    )
