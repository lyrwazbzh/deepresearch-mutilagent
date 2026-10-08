from __future__ import annotations

import builtins
import hashlib
import inspect
import time
from datetime import timedelta
from functools import wraps
from typing import Any, Iterable, Literal, cast

from sqlalchemy import delete, select, text
from sqlalchemy.exc import IntegrityError

from src.agent.config import Settings
from src.agent.memory.domain import (
    MemoryCandidate,
    MemoryRecord,
    MemorySource,
    MemoryStatus,
    MemoryWriteResult,
    RecallScore,
    TopicRegistry,
    canonical_memory_content,
    cosine_similarity,
    lexical_score,
    memory_content_hash,
    memory_write_key,
)
from src.agent.schema.model_config import FrozenModelBinding
from src.agent.schema.state import MemoryContext, MemoryItemRef
from src.agent.llm.runtime import ModelResolver
from src.agent.backends.persistence.database import SessionFactory
from src.agent.backends.persistence.models import (
    AuditLogRow,
    ConversationMessageRow,
    OutboxMessageRow,
    ResearchMemoryRow,
    utcnow,
)
from src.agent.backends.persistence.repositories import MemoryRepository
from src.agent.backends.observability import Telemetry, TraceScope, add_span_event
from src.agent.errors import (
    IdempotencyConflictError,
    InvalidArgumentError,
    NotFoundError,
    StaleConflictError,
)
from src.agent.task_service import new_id


MemoryDisposition = Literal["created", "exact_dedup", "replacement", "pending_conflict"]


def _observe_memory(operation: str):
    def decorate(method: Any) -> Any:
        signature = inspect.signature(method)

        @wraps(method)
        def observed(self: "MemoryService", *args: Any, **kwargs: Any) -> Any:
            if self.telemetry is None:
                return method(self, *args, **kwargs)
            bound = signature.bind(self, *args, **kwargs)
            tenant_id = str(bound.arguments.get("tenant_id") or "system")
            user_id = bound.arguments.get("user_id")
            started = time.monotonic()
            status = "succeeded"
            try:
                with self.telemetry.span(
                    "memory.operation",
                    scope=TraceScope(
                        tenant_id=tenant_id,
                        user_id=str(user_id) if user_id else None,
                    ),
                    attributes={"memory.operation": operation},
                ) as span:
                    result = method(self, *args, **kwargs)
                    if span is not None:
                        span.set_attribute("memory.result", _memory_result(result))
                    return result
            except Exception as exc:
                status = "failed"
                if isinstance(exc, StaleConflictError):
                    self.telemetry.metrics.add("memory_stale_conflict_total")
                raise
            finally:
                self.telemetry.metrics.add(
                    "memory_operation_duration_seconds",
                    time.monotonic() - started,
                    operation=operation,
                    status=status,
                )
                self._refresh_observability_gauges()

        return observed

    return decorate


def _memory_result(result: Any) -> str:
    if isinstance(result, MemoryWriteResult):
        return result.disposition
    if isinstance(result, MemoryContext):
        return "recalled" if result.items else "empty"
    if isinstance(result, bool):
        return "updated" if result else "stale"
    return "succeeded"


class MemoryService:
    """Single transactional service for Research LTM writes, recall and lifecycle."""

    def __init__(
        self,
        session_factory: SessionFactory,
        settings: Settings,
        model_resolver: ModelResolver,
        *,
        topics: TopicRegistry | None = None,
        telemetry: Telemetry | None = None,
    ) -> None:
        self.session_factory = session_factory
        self.settings = settings
        self.model_resolver = model_resolver
        self.topics = topics or TopicRegistry()
        self.telemetry = telemetry
        self._scope_versions: dict[tuple[str, str], int] = {}

    @_observe_memory("remember")
    def remember(
        self,
        *,
        tenant_id: str,
        user_id: str,
        topic: str,
        content: str,
        supporting_message_ids: builtins.list[str],
        logical_key: str,
        source: MemorySource = "explicit_remember",
        confidence: float | None = None,
    ) -> MemoryWriteResult:
        if source not in {"explicit_remember", "turn_completion", "explicit_correction"}:
            raise InvalidArgumentError("remember does not accept this source")
        confidence = 1.0 if source != "turn_completion" else float(confidence or 0)
        return self._write(
            tenant_id=tenant_id,
            user_id=user_id,
            topic=topic,
            content=content,
            supporting_message_ids=supporting_message_ids,
            logical_key=logical_key,
            source=source,
            confidence=confidence,
        )

    @_observe_memory("extract")
    def extract_batch(
        self,
        *,
        tenant_id: str,
        user_id: str,
        candidates: Iterable[MemoryCandidate],
        user_messages: dict[str, str],
        input_hash: str,
    ) -> builtins.list[MemoryWriteResult]:
        results: builtins.list[MemoryWriteResult] = []
        for index, candidate in enumerate(list(candidates)[:5]):
            if candidate.confidence < self.settings.ltm_write_confidence_threshold:
                continue
            try:
                definition = self.topics.get(candidate.topic)
            except ValueError:
                continue
            if definition.category != candidate.category:
                continue
            support_ids = list(dict.fromkeys(candidate.supporting_user_message_ids))
            if not support_ids or any(item not in user_messages for item in support_ids):
                continue
            support = " ".join(user_messages[item] for item in support_ids)
            if lexical_score(support, candidate.content) <= 0:
                continue
            try:
                result = self._write(
                    tenant_id=tenant_id,
                    user_id=user_id,
                    topic=definition.topic,
                    content=candidate.content,
                    supporting_message_ids=support_ids,
                    logical_key=f"extract:{input_hash}:{index}",
                    source="turn_completion",
                    confidence=candidate.confidence,
                )
            except InvalidArgumentError:
                continue
            results.append(result)
        return results

    def manual_edit(
        self,
        *,
        tenant_id: str,
        user_id: str,
        memory_id: str,
        expected_revision: int,
        content: str,
        topic: str | None = None,
        supporting_message_ids: builtins.list[str] | None = None,
        logical_key: str,
        source: MemorySource = "manual_edit",
    ) -> MemoryWriteResult:
        if source not in {"manual_edit", "explicit_correction"}:
            raise InvalidArgumentError("manual edit source is invalid")
        try:
            with self.session_factory() as session, session.begin():
                repository = MemoryRepository(session, tenant_id, user_id)
                old = repository.get(memory_id, for_update=True)
                if old is None:
                    raise NotFoundError("Memory was not found.")
                if old.status != "active" or old.revision != expected_revision:
                    raise StaleConflictError("Memory revision or status changed.")
                if topic is not None:
                    try:
                        canonical_topic = self.topics.normalize(topic)
                    except ValueError as exc:
                        raise InvalidArgumentError(str(exc)) from exc
                    if canonical_topic != old.topic:
                        raise InvalidArgumentError(
                            "Manual edit cannot move a memory to another canonical topic."
                        )
                messages = supporting_message_ids or list(old.supporting_message_ids or [])
                self._assert_supporting_messages(session, tenant_id, user_id, messages)
                result = self._write_in_session(
                    session=session,
                    tenant_id=tenant_id,
                    user_id=user_id,
                    topic=old.topic,
                    content=content,
                    supporting_message_ids=messages,
                    logical_key=logical_key,
                    source=source,
                    confidence=1.0,
                    authoritative_old=old,
                )
                self._audit(
                    session,
                    tenant_id,
                    user_id,
                    "memory.manual_edit"
                    if source == "manual_edit"
                    else "memory.explicit_correction",
                    result.memory.memory_id,
                )
            self._invalidate(tenant_id, user_id)
            return result
        except IntegrityError as exc:
            raise StaleConflictError("Concurrent memory edit conflicted.") from exc

    def correct(
        self,
        *,
        tenant_id: str,
        user_id: str,
        memory_id: str,
        expected_revision: int,
        content: str,
        supporting_message_ids: builtins.list[str],
        logical_key: str,
    ) -> MemoryWriteResult:
        return self.manual_edit(
            tenant_id=tenant_id,
            user_id=user_id,
            memory_id=memory_id,
            expected_revision=expected_revision,
            content=content,
            supporting_message_ids=supporting_message_ids,
            logical_key=logical_key,
            source="explicit_correction",
        )

    @_observe_memory("resolve")
    def resolve(
        self,
        *,
        tenant_id: str,
        user_id: str,
        memory_id: str,
        action: str,
        expected_revision: int,
        related_expected_revision: int,
    ) -> MemoryRecord:
        if action not in {"accept_new", "keep_old"}:
            raise InvalidArgumentError("Unknown conflict resolution action.")
        try:
            with self.session_factory() as session, session.begin():
                repository = MemoryRepository(session, tenant_id, user_id)
                candidate = repository.get(memory_id, for_update=True)
                if candidate is None:
                    raise NotFoundError("Pending memory conflict was not found.")
                related = (
                    repository.get(candidate.related_memory_id, for_update=True)
                    if candidate.related_memory_id
                    else None
                )
                if related is not None:
                    accept_replay = (
                        action == "accept_new"
                        and candidate.status == "active"
                        and related.status == "superseded"
                        and candidate.revision == expected_revision + 1
                        and related.revision == related_expected_revision + 1
                    )
                    keep_replay = (
                        action == "keep_old"
                        and candidate.status == "superseded"
                        and related.status == "active"
                        and candidate.revision == expected_revision + 1
                        and related.revision == related_expected_revision
                    )
                    if accept_replay or keep_replay:
                        return self._record(candidate)
                if candidate.status != "pending_conflict":
                    raise NotFoundError("Pending memory conflict was not found.")
                if related is None or related.status != "active":
                    raise StaleConflictError("Related active memory changed.")
                if (
                    candidate.revision != expected_revision
                    or related.revision != related_expected_revision
                ):
                    raise StaleConflictError("Memory conflict revisions are stale.")
                now = utcnow()
                if action == "accept_new":
                    related.status = "superseded"
                    related.revision += 1
                    related.updated_at = now
                    session.flush()
                    candidate.status = "active"
                else:
                    candidate.status = "superseded"
                candidate.revision += 1
                candidate.updated_at = now
                session.flush()
                if action == "accept_new" and candidate.embedding is None:
                    binding = self.model_resolver.resolve_snapshot(
                        session, tenant_id, ("embedding",)
                    ).get("embedding")
                    self._request_embedding(session, candidate, binding)
                result = self._record(candidate)
                self._audit(
                    session,
                    tenant_id,
                    user_id,
                    f"memory.resolve.{action}",
                    candidate.memory_id,
                )
            self._invalidate(tenant_id, user_id)
            return result
        except IntegrityError as exc:
            raise StaleConflictError("Concurrent conflict resolution changed the topic.") from exc

    @_observe_memory("forget")
    def forget(
        self,
        *,
        tenant_id: str,
        user_id: str,
        memory_id: str,
        expected_revision: int,
    ) -> MemoryRecord:
        with self.session_factory() as session, session.begin():
            repository = MemoryRepository(session, tenant_id, user_id)
            row = repository.get(memory_id, for_update=True)
            if row is None:
                raise NotFoundError("Memory was not found.")
            if row.status == "forgotten":
                return self._record(row)
            if row.revision != expected_revision:
                raise StaleConflictError("Memory revision is stale.")
            row.status = "forgotten"
            row.revision += 1
            row.updated_at = utcnow()
            session.add(
                OutboxMessageRow(
                    message_id=new_id("outbox"),
                    tenant_id=tenant_id,
                    kind="memory.gc",
                    aggregate_id=row.memory_id,
                    payload_json={
                        "tenant_id": tenant_id,
                        "user_id": user_id,
                        "memory_id": row.memory_id,
                    },
                    idempotency_key=f"memory:{row.memory_id}:forgotten:{row.revision}",
                    available_at=utcnow() + timedelta(hours=24),
                )
            )
            self._audit(
                session, tenant_id, user_id, "memory.forget", row.memory_id
            )
            session.flush()
            result = self._record(row)
        self._invalidate(tenant_id, user_id)
        return result

    def list(
        self,
        *,
        tenant_id: str,
        user_id: str,
        status: str | None = None,
        category: str | None = None,
        topic: str | None = None,
        limit: int = 100,
    ) -> builtins.list[MemoryRecord]:
        if status is not None and status not in {
            "active",
            "pending_conflict",
            "superseded",
            "forgotten",
        }:
            raise InvalidArgumentError("Unknown memory status.")
        try:
            canonical_topic = self.topics.normalize(topic) if topic else None
        except ValueError as exc:
            raise InvalidArgumentError(str(exc)) from exc
        allowed_categories = {item.category for item in self.topics.definitions}
        if category is not None and category not in allowed_categories:
            raise InvalidArgumentError("Unknown memory category.")
        with self.session_factory() as session:
            rows = MemoryRepository(session, tenant_id, user_id).list(
                status=status, topic=canonical_topic, limit=min(max(limit, 1), 100)
            )
            records = [self._record(row) for row in rows]
        if category is not None:
            records = [item for item in records if item.category == category]
        return records

    @_observe_memory("recall")
    def recall(
        self,
        *,
        tenant_id: str,
        user_id: str,
        planning_query: str,
        query_embedding: builtins.list[float] | None = None,
    ) -> MemoryContext:
        query_topics = self.topics.topics_for_query(planning_query)
        if query_embedding is None and self.telemetry is not None:
            add_span_event("memory.degraded", {"reason_code": "embedding_unavailable"})
            self.telemetry.metrics.add(
                "memory_recall_degraded_total", reason_code="embedding_unavailable"
            )
        with self.session_factory() as session:
            rows = MemoryRepository(session, tenant_id, user_id).active()
            semantic_scores: dict[str, float] = {}
            if (
                query_embedding
                and session.bind is not None
                and session.bind.dialect.name == "postgresql"
            ):
                rendered_vector = "[" + ",".join(
                    format(float(item), ".12g") for item in query_embedding
                ) + "]"
                semantic_scores = {
                    str(memory_id): float(score)
                    for memory_id, score in session.execute(
                        text(
                            """
                            SELECT memory_id,
                                   1 - (embedding <=> CAST(:query_vector AS vector)) AS score
                            FROM research_memories
                            WHERE tenant_id = :tenant_id
                              AND user_id = :user_id
                              AND status = 'active'
                              AND embedding IS NOT NULL
                              AND 1 - (embedding <=> CAST(:query_vector AS vector)) >= :minimum
                            ORDER BY embedding <=> CAST(:query_vector AS vector)
                            LIMIT :candidate_k
                            """
                        ),
                        {
                            "query_vector": rendered_vector,
                            "tenant_id": tenant_id,
                            "user_id": user_id,
                            "minimum": self.settings.ltm_min_similarity,
                            "candidate_k": self.settings.ltm_candidate_k,
                        },
                    ).all()
                }
            scored: builtins.list[tuple[RecallScore, ResearchMemoryRow]] = []
            for row in rows:
                definition = self.topics.get(row.topic)
                exact = row.topic in query_topics
                semantic = semantic_scores.get(
                    row.memory_id, cosine_similarity(query_embedding, row.embedding)
                )
                lexical = lexical_score(planning_query, row.content)
                if not (
                    definition.base_memory
                    or exact
                    or semantic >= self.settings.ltm_min_similarity
                    or lexical >= self.settings.ltm_min_lexical_score
                ):
                    continue
                relevance = 0.70 * semantic + 0.20 * lexical + 0.10 * float(row.confidence)
                scored.append(
                    (
                        RecallScore(
                            memory_id=row.memory_id,
                            base_memory=definition.base_memory,
                            exact_topic=exact,
                            semantic_score=semantic,
                            lexical_score=lexical,
                            relevance_score=relevance,
                        ),
                        row,
                    )
                )
            scored.sort(
                key=lambda item: (
                    item[0].base_memory,
                    item[0].exact_topic,
                    item[0].relevance_score,
                    item[1].updated_at,
                    item[1].memory_id,
                ),
                reverse=True,
            )
            candidates = scored[: self.settings.ltm_candidate_k]
            selected: builtins.list[MemoryItemRef] = []
            token_count = 0
            seen: set[str] = set()
            for _score, row in candidates:
                if row.memory_id in seen or len(selected) >= self.settings.ltm_top_k:
                    continue
                item_tokens = _estimated_tokens(row.content)
                if token_count + item_tokens > self.settings.ltm_token_budget:
                    continue
                selected.append(
                    MemoryItemRef(
                        memory_id=row.memory_id,
                        revision=row.revision,
                        content_hash=row.content_hash,
                    )
                )
                seen.add(row.memory_id)
                token_count += item_tokens
        return MemoryContext(
            query_hash=hashlib.sha256(planning_query.encode("utf-8")).hexdigest(),
            items=selected,
        )

    def rehydrate(
        self,
        tenant_id: str,
        user_id: str,
        context: MemoryContext | None,
    ) -> builtins.list[dict[str, Any]]:
        if context is None:
            return []
        rendered: builtins.list[dict[str, Any]] = []
        token_count = 0
        with self.session_factory() as session:
            repository = MemoryRepository(session, tenant_id, user_id)
            for reference in context.items:
                row = repository.get(reference.memory_id)
                if (
                    row is None
                    or row.status != "active"
                    or row.revision != reference.revision
                    or row.content_hash != reference.content_hash
                ):
                    continue
                item_tokens = _estimated_tokens(row.content)
                if token_count + item_tokens > self.settings.ltm_token_budget:
                    break
                definition = self.topics.get(row.topic)
                rendered.append(
                    {
                        "memory_id": row.memory_id,
                        "topic": row.topic,
                        "category": definition.category,
                        "content": row.content,
                    }
                )
                token_count += item_tokens
        return rendered

    @_observe_memory("embed")
    def set_embedding(
        self,
        *,
        tenant_id: str,
        user_id: str,
        memory_id: str,
        expected_revision: int,
        embedding: builtins.list[float],
    ) -> bool:
        with self.session_factory() as session, session.begin():
            row = MemoryRepository(session, tenant_id, user_id).get(memory_id, for_update=True)
            if row is None or row.revision != expected_revision:
                return False
            if row.status in {"forgotten", "superseded"}:
                return False
            row.embedding = [float(item) for item in embedding]
            row.updated_at = utcnow()
        self._invalidate(tenant_id, user_id)
        return True

    def gc(self) -> int:
        now = utcnow()
        forgotten_before = now - timedelta(hours=24)
        superseded_before = now - timedelta(days=180)
        with self.session_factory() as session, session.begin():
            result = session.execute(
                delete(ResearchMemoryRow).where(
                    (
                        (ResearchMemoryRow.status == "forgotten")
                        & (ResearchMemoryRow.updated_at <= forgotten_before)
                    )
                    | (
                        (ResearchMemoryRow.status == "superseded")
                        & (ResearchMemoryRow.updated_at <= superseded_before)
                    )
                )
            )
            return int(result.rowcount or 0)

    def delete_scope(self, *, tenant_id: str, user_id: str | None = None) -> int:
        """Hard-delete LTM during user or tenant lifecycle deletion.

        Task deletion deliberately does not call this method: research memory belongs to
        the user scope, not to an individual task.
        """
        predicate = ResearchMemoryRow.tenant_id == tenant_id
        if user_id is not None:
            predicate &= ResearchMemoryRow.user_id == user_id
        with self.session_factory() as session, session.begin():
            affected_users = set(
                session.scalars(
                    select(ResearchMemoryRow.user_id).where(predicate).distinct()
                ).all()
            )
            result = session.execute(delete(ResearchMemoryRow).where(predicate))
        for affected_user_id in affected_users:
            self._invalidate(tenant_id, affected_user_id)
        return int(result.rowcount or 0)

    def recover_pending_embeddings(
        self, *, limit: int = 100, older_than_seconds: int = 300
    ) -> int:
        """Re-enqueue durable rows left without vectors after exhausted worker delivery."""
        now = utcnow()
        recovery_bucket = int(now.timestamp() // 3600)
        count = 0
        with self.session_factory() as session, session.begin():
            rows = list(
                session.scalars(
                    select(ResearchMemoryRow)
                    .where(
                        ResearchMemoryRow.embedding.is_(None),
                        ResearchMemoryRow.status.in_(("active", "pending_conflict")),
                        ResearchMemoryRow.updated_at
                        <= now - timedelta(seconds=max(0, older_than_seconds)),
                    )
                    .order_by(ResearchMemoryRow.updated_at, ResearchMemoryRow.memory_id)
                    .limit(limit)
                    .with_for_update(skip_locked=True)
                ).all()
            )
            bindings: dict[str, FrozenModelBinding] = {}
            for row in rows:
                binding = bindings.get(row.tenant_id)
                if binding is None:
                    binding = self.model_resolver.resolve_snapshot(
                        session, row.tenant_id, ("embedding",)
                    ).get("embedding")
                    bindings[row.tenant_id] = binding
                key = (
                    f"memory:{row.memory_id}:embedding:{row.revision}:"
                    f"recovery:{recovery_bucket}"
                )
                existing = session.scalar(
                    select(OutboxMessageRow.message_id).where(
                        OutboxMessageRow.tenant_id == row.tenant_id,
                        OutboxMessageRow.kind == "memory.embedding.requested",
                        OutboxMessageRow.idempotency_key == key,
                    )
                )
                if existing is not None:
                    continue
                session.add(
                    OutboxMessageRow(
                        message_id=new_id("outbox"),
                        tenant_id=row.tenant_id,
                        kind="memory.embedding.requested",
                        aggregate_id=row.memory_id,
                        payload_json={
                            "tenant_id": row.tenant_id,
                            "user_id": row.user_id,
                            "memory_id": row.memory_id,
                            "revision": row.revision,
                            "binding": binding.model_dump(mode="json"),
                        },
                        idempotency_key=key,
                        available_at=now,
                    )
                )
                count += 1
        return count

    def scope_etag(self, tenant_id: str, user_id: str) -> str:
        version = self._scope_versions.get((tenant_id, user_id), 0)
        return hashlib.sha256(f"{tenant_id}|{user_id}|{version}".encode()).hexdigest()

    def _refresh_observability_gauges(self) -> None:
        if self.telemetry is None:
            return
        now = utcnow()
        try:
            with self.session_factory() as session:
                pending = len(
                    session.scalars(
                        select(ResearchMemoryRow.memory_id).where(
                            ResearchMemoryRow.status == "pending_conflict"
                        )
                    ).all()
                )
                oldest = session.scalar(
                    select(ResearchMemoryRow.updated_at)
                    .where(
                        ResearchMemoryRow.embedding.is_(None),
                        ResearchMemoryRow.status.in_(("active", "pending_conflict")),
                    )
                    .order_by(ResearchMemoryRow.updated_at)
                    .limit(1)
                )
            self.telemetry.metrics.set("memory_pending_conflicts", float(pending))
            oldest_seconds = (
                max(0.0, (now - oldest).total_seconds()) if oldest is not None else 0.0
            )
            self.telemetry.metrics.set(
                "memory_embedding_pending_oldest_seconds", oldest_seconds
            )
        except Exception:
            # Metrics are never allowed to change Memory semantics.
            return

    def _write(
        self,
        *,
        tenant_id: str,
        user_id: str,
        topic: str,
        content: str,
        supporting_message_ids: builtins.list[str],
        logical_key: str,
        source: MemorySource,
        confidence: float,
    ) -> MemoryWriteResult:
        try:
            with self.session_factory() as session, session.begin():
                self._assert_supporting_messages(
                    session, tenant_id, user_id, supporting_message_ids
                )
                result = self._write_in_session(
                    session=session,
                    tenant_id=tenant_id,
                    user_id=user_id,
                    topic=topic,
                    content=content,
                    supporting_message_ids=supporting_message_ids,
                    logical_key=logical_key,
                    source=source,
                    confidence=confidence,
                )
                self._audit(
                    session,
                    tenant_id,
                    user_id,
                    f"memory.{source}",
                    result.memory.memory_id,
                    system=source == "turn_completion",
                )
            self._invalidate(tenant_id, user_id)
            return result
        except IntegrityError as exc:
            with self.session_factory() as session:
                definition = self.topics.get(topic)
                digest = memory_content_hash(content)
                key = memory_write_key(
                    tenant_id=tenant_id,
                    user_id=user_id,
                    source=source,
                    topic=definition.topic,
                    content_hash=digest,
                    supporting_message_ids=supporting_message_ids,
                    logical_key=logical_key,
                )
                repository = MemoryRepository(session, tenant_id, user_id)
                row = repository.by_write_key(key) or repository.exact_live(
                    definition.topic, digest
                )
                if row is not None:
                    return MemoryWriteResult(
                        memory=self._record(row), disposition="exact_dedup"
                    )
            raise StaleConflictError("Concurrent memory write conflicted.") from exc

    def _write_in_session(
        self,
        *,
        session: Any,
        tenant_id: str,
        user_id: str,
        topic: str,
        content: str,
        supporting_message_ids: builtins.list[str],
        logical_key: str,
        source: MemorySource,
        confidence: float,
        authoritative_old: ResearchMemoryRow | None = None,
    ) -> MemoryWriteResult:
        try:
            definition = self.topics.get(topic)
        except ValueError as exc:
            raise InvalidArgumentError(str(exc)) from exc
        canonical = canonical_memory_content(content)
        if not canonical or len(canonical) > self.settings.ltm_max_content_chars:
            raise InvalidArgumentError("Memory content is empty or exceeds the configured limit.")
        if not 0 <= confidence <= 1:
            raise InvalidArgumentError("Memory confidence must be between zero and one.")
        if source == "turn_completion" and confidence < self.settings.ltm_write_confidence_threshold:
            raise InvalidArgumentError("Automatic memory confidence is below the write threshold.")
        digest = memory_content_hash(canonical)
        key = memory_write_key(
            tenant_id=tenant_id,
            user_id=user_id,
            source=source,
            topic=definition.topic,
            content_hash=digest,
            supporting_message_ids=supporting_message_ids,
            logical_key=logical_key,
        )
        repository = MemoryRepository(session, tenant_id, user_id)
        replay = repository.by_write_key(key)
        if replay is not None:
            if replay.content_hash != digest:
                raise IdempotencyConflictError(
                    "Memory write identity was replayed with different content."
                )
            return MemoryWriteResult(memory=self._record(replay), disposition="exact_dedup")
        exact = repository.exact_live(definition.topic, digest)
        if exact is not None:
            exact.confidence = max(float(exact.confidence), confidence)
            exact.supporting_message_ids = list(
                dict.fromkeys([*(exact.supporting_message_ids or []), *supporting_message_ids])
            )
            exact.revision += 1
            exact.updated_at = utcnow()
            session.flush()
            if exact.embedding is None:
                binding = self.model_resolver.resolve_snapshot(
                    session, tenant_id, ("embedding",)
                ).get("embedding")
                self._request_embedding(session, exact, binding)
            return MemoryWriteResult(memory=self._record(exact), disposition="exact_dedup")
        active = authoritative_old or repository.active_topic(
            definition.topic, for_update=True
        )
        authoritative = source in {
            "explicit_remember",
            "explicit_correction",
            "manual_edit",
        }
        now = utcnow()
        status = "active"
        related_memory_id: str | None = None
        disposition: MemoryDisposition = "created"
        if active is not None:
            related_memory_id = active.memory_id
            if authoritative:
                active.status = "superseded"
                active.revision += 1
                active.updated_at = now
                session.flush()
                disposition = "replacement"
            else:
                status = "pending_conflict"
                disposition = "pending_conflict"
        row = ResearchMemoryRow(
            memory_id=new_id("memory"),
            tenant_id=tenant_id,
            user_id=user_id,
            topic=definition.topic,
            content=canonical,
            source=source,
            supporting_message_ids=list(dict.fromkeys(supporting_message_ids)),
            confidence=confidence,
            embedding=None,
            content_hash=digest,
            write_key=key,
            status=status,
            related_memory_id=related_memory_id,
            revision=1,
            created_at=now,
            updated_at=now,
        )
        repository.add(row)
        session.flush()
        binding = self.model_resolver.resolve_snapshot(
            session, tenant_id, ("embedding",)
        ).get("embedding")
        self._request_embedding(session, row, binding)
        return MemoryWriteResult(memory=self._record(row), disposition=disposition)

    @staticmethod
    def _assert_supporting_messages(
        session: Any,
        tenant_id: str,
        user_id: str,
        message_ids: builtins.list[str],
    ) -> None:
        unique: set[str] = set(message_ids)
        if not unique:
            raise InvalidArgumentError("At least one supporting user message is required.")
        rows = session.scalars(
            select(ConversationMessageRow).where(
                ConversationMessageRow.tenant_id == tenant_id,
                ConversationMessageRow.user_id == user_id,
                ConversationMessageRow.message_id.in_(unique),
                ConversationMessageRow.role == "user",
            )
        ).all()
        if {row.message_id for row in rows} != unique:
            raise InvalidArgumentError(
                "Supporting messages must be accessible user messages in the same scope."
            )

    @staticmethod
    def _request_embedding(
        session: Any,
        row: ResearchMemoryRow,
        binding: FrozenModelBinding,
    ) -> None:
        session.add(
            OutboxMessageRow(
                message_id=new_id("outbox"),
                tenant_id=row.tenant_id,
                kind="memory.embedding.requested",
                aggregate_id=row.memory_id,
                payload_json={
                    "tenant_id": row.tenant_id,
                    "user_id": row.user_id,
                    "memory_id": row.memory_id,
                    "revision": row.revision,
                    "binding": binding.model_dump(mode="json"),
                },
                idempotency_key=f"memory:{row.memory_id}:embedding:{row.revision}",
                available_at=utcnow(),
            )
        )

    def _record(self, row: ResearchMemoryRow) -> MemoryRecord:
        definition = self.topics.get(row.topic)
        return MemoryRecord(
            memory_id=row.memory_id,
            tenant_id=row.tenant_id,
            user_id=row.user_id,
            topic=row.topic,
            category=definition.category,
            content=row.content,
            source=cast(MemorySource, row.source),
            supporting_message_ids=list(row.supporting_message_ids or []),
            confidence=float(row.confidence),
            content_hash=row.content_hash,
            write_key=row.write_key,
            status=cast(MemoryStatus, row.status),
            related_memory_id=row.related_memory_id,
            revision=row.revision,
            embedding_status="ready" if row.embedding is not None else "pending",
            created_at=row.created_at,
            updated_at=row.updated_at,
        )

    def _invalidate(self, tenant_id: str, user_id: str) -> None:
        scope = (tenant_id, user_id)
        self._scope_versions[scope] = self._scope_versions.get(scope, 0) + 1

    @staticmethod
    def _audit(
        session: Any,
        tenant_id: str,
        user_id: str,
        action: str,
        memory_id: str,
        *,
        system: bool = False,
    ) -> None:
        session.add(
            AuditLogRow(
                audit_id=new_id("audit"),
                tenant_id=tenant_id,
                actor_ref=("system:memory-extractor" if system else f"user:{user_id}"),
                action=action,
                resource_ref=f"memory:{memory_id}",
                result="succeeded",
            )
        )


def _estimated_tokens(text: str) -> int:
    return max(1, (len(text) + 3) // 4)
