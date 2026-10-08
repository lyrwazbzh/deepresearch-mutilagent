from __future__ import annotations

import hashlib
import json
from collections import deque
from contextlib import nullcontext
from datetime import timedelta
from threading import Lock
from typing import Any, Literal, Protocol, cast

import httpx
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import select

from src.agent.identity import canonical_json
from src.agent.schema.model_config import ModelBindingSnapshot
from src.agent.schema.runtime import ExecutionContext, NodeResult
from src.agent.schema.state import Evidence, ResearchState
from src.agent.backends.persistence.database import SessionFactory
from src.agent.backends.persistence.models import (
    OutboxMessageRow,
    NodeRunRow,
    ResearchCheckpointRow,
    ResearchTaskRow,
    TaskKnowledgeGraphRow,
    utcnow,
)
from src.agent.runtime.tool_runtime import (
    ToolAdapter,
    ToolDefinition,
    ToolError,
    ToolExecutionContext,
    ToolResult,
)
from src.agent.runtime.budget import bind_runtime_effect_context
from src.agent.errors import InvalidArgumentError, NotFoundError
from src.agent.task_service import new_id


ENTITY_TYPES = frozenset(
    {
        "Organization",
        "Person",
        "Product",
        "Technology",
        "Method",
        "Dataset",
        "Metric",
        "Event",
        "Location",
        "Policy",
        "Concept",
    }
)


def _normalized(value: str) -> str:
    return " ".join(value.casefold().strip().split())


def _stable_id(prefix: str, *parts: str) -> str:
    digest = hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()
    return f"{prefix}_{digest[:32]}"


def _question_hashes(state: ResearchState) -> tuple[str, str]:
    query_hash = hashlib.sha256(_normalized(state.query).encode()).hexdigest()
    hypotheses = (
        [
            {"hypothesis_id": item.hypothesis_id, "claim": item.claim}
            for item in state.plan.hypotheses
        ]
        if state.plan
        else []
    )
    plan_hash = hashlib.sha256(canonical_json(hypotheses)).hexdigest()
    return query_hash, plan_hash


class GraphEntity(BaseModel):
    model_config = ConfigDict(extra="forbid")

    entity_id: str
    canonical_name: str
    entity_type: str
    aliases: list[str] = Field(default_factory=list)


class GraphProvenance(BaseModel):
    model_config = ConfigDict(extra="forbid")

    evidence_id: str
    locator: str | None = None


class GraphClaim(BaseModel):
    model_config = ConfigDict(extra="forbid")

    claim_id: str
    claim_key: str
    predicate: str
    subject_entity_id: str | None = None
    object_entity_id: str | None = None
    value: str | None = None
    qualifiers: dict[str, str] = Field(default_factory=dict)
    provenance: list[GraphProvenance] = Field(min_length=1)


class GraphRelation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    from_entity_id: str
    to_entity_id: str | None = None
    predicate: str
    claim_id: str


class GraphPath(BaseModel):
    model_config = ConfigDict(extra="forbid")

    entity_ids: list[str]
    claim_ids: list[str]
    hops: int


class ExtractedEntity(BaseModel):
    model_config = ConfigDict(extra="forbid")

    canonical_name: str = Field(min_length=1)
    entity_type: str = "Concept"
    aliases: list[str] = Field(default_factory=list)


class ExtractedClaim(BaseModel):
    model_config = ConfigDict(extra="forbid")

    predicate: str = Field(min_length=1)
    subject: str | None = None
    object: str | None = None
    value: str | None = None
    qualifiers: dict[str, str] = Field(default_factory=dict)
    evidence_id: str
    locator: str | None = None

    @model_validator(mode="after")
    def has_object_or_value(self) -> "ExtractedClaim":
        if not self.object and not self.value:
            raise ValueError("claim requires object or value")
        return self


class ExtractedGraph(BaseModel):
    model_config = ConfigDict(extra="forbid")

    entities: list[ExtractedEntity] = Field(default_factory=list)
    claims: list[ExtractedClaim] = Field(default_factory=list)


class KnowledgeGraphBuildResult(BaseModel):
    status: Literal["ready", "partial"]
    entity_count: int
    claim_count: int
    relation_count: int
    processed_evidence_count: int
    skipped_evidence_ids: list[str] = Field(default_factory=list)
    added: dict[str, int] = Field(default_factory=dict)
    conflicts_kept: int = 0
    ungrounded_dropped: int = 0
    stale_discarded: bool = False


class KnowledgeGraphQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")

    op: Literal["neighbors", "path", "search"]
    entity: str | None = None
    target_entity: str | None = None
    query: str | None = None
    relation: str | None = None
    max_hops: int = Field(default=3, ge=1, le=5)
    limit: int = Field(default=20, ge=1, le=100)

    @model_validator(mode="after")
    def required_arguments(self) -> "KnowledgeGraphQuery":
        if self.op == "neighbors" and not self.entity:
            raise ValueError("neighbors requires entity")
        if self.op == "path" and (not self.entity or not self.target_entity):
            raise ValueError("path requires entity and target_entity")
        if self.op == "search" and not self.query:
            raise ValueError("search requires query")
        return self


class KnowledgeGraphQueryResult(BaseModel):
    graph_status: Literal["building", "ready", "partial"]
    resolution_status: Literal["hit", "miss", "ambiguous"] | None = None
    candidates: list[GraphEntity] = Field(default_factory=list)
    entities: list[GraphEntity] = Field(default_factory=list)
    claims: list[GraphClaim] = Field(default_factory=list)
    relations: list[GraphRelation] = Field(default_factory=list)
    paths: list[GraphPath] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)
    truncated: bool = False


class KnowledgeExtractor(Protocol):
    def extract(
        self,
        *,
        tenant_id: str,
        bindings: ModelBindingSnapshot,
        query: str,
        hypotheses: list[dict[str, str]],
        evidence: Evidence,
        content: str,
    ) -> ExtractedGraph: ...


class ModelKnowledgeExtractor:
    def __init__(self, model_runtime: object) -> None:
        self.model_runtime = model_runtime

    def extract(
        self,
        *,
        tenant_id: str,
        bindings: ModelBindingSnapshot,
        query: str,
        hypotheses: list[dict[str, str]],
        evidence: Evidence,
        content: str,
    ) -> ExtractedGraph:
        model = self.model_runtime.build_chat_model_for(  # type: ignore[attr-defined]
            tenant_id, bindings.get("reasoning"), "task_knowledge_graph"
        )
        prompt = (
            "Extract only atomic, question-relevant and explicitly grounded entities and "
            "claims. Every claim must use the supplied evidence_id. Do not infer facts.\n"
            f"Research query: {query}\nHypotheses: {canonical_json(hypotheses).decode()}\n"
            f"Evidence metadata: {evidence.model_dump_json(exclude={'data'})}\n"
            f"evidence_id: {evidence.evidence_id}\nContent:\n{content[:24000]}"
        )
        return model.invoke_structured(prompt, ExtractedGraph)


class KnowledgeGraphStore(Protocol):
    def clear(self, tenant_id: str, task_id: str) -> None: ...

    def processed_evidence_ids(self, tenant_id: str, task_id: str) -> set[str]: ...

    def upsert(
        self,
        tenant_id: str,
        task_id: str,
        entities: list[GraphEntity],
        claims: list[GraphClaim],
    ) -> tuple[int, int]: ...

    def snapshot(
        self, tenant_id: str, task_id: str
    ) -> tuple[list[GraphEntity], list[GraphClaim]]: ...


class InMemoryKnowledgeGraphStore:
    def __init__(self) -> None:
        self._graphs: dict[
            tuple[str, str], tuple[dict[str, GraphEntity], dict[str, GraphClaim]]
        ] = {}

    def _graph(
        self, tenant_id: str, task_id: str
    ) -> tuple[dict[str, GraphEntity], dict[str, GraphClaim]]:
        return self._graphs.setdefault((tenant_id, task_id), ({}, {}))

    def clear(self, tenant_id: str, task_id: str) -> None:
        self._graphs.pop((tenant_id, task_id), None)

    def processed_evidence_ids(self, tenant_id: str, task_id: str) -> set[str]:
        _, claims = self._graph(tenant_id, task_id)
        return {
            item.evidence_id
            for claim in claims.values()
            for item in claim.provenance
        }

    def upsert(
        self,
        tenant_id: str,
        task_id: str,
        entities: list[GraphEntity],
        claims: list[GraphClaim],
    ) -> tuple[int, int]:
        stored_entities, stored_claims = self._graph(tenant_id, task_id)
        added_entities = 0
        added_claims = 0
        for entity in entities:
            prior_entity = stored_entities.get(entity.entity_id)
            if prior_entity is None:
                stored_entities[entity.entity_id] = entity.model_copy(deep=True)
                added_entities += 1
            else:
                aliases = sorted(set(prior_entity.aliases) | set(entity.aliases))
                stored_entities[entity.entity_id] = prior_entity.model_copy(
                    update={"aliases": aliases}
                )
        for claim in claims:
            prior_claim = stored_claims.get(claim.claim_key)
            if prior_claim is None:
                stored_claims[claim.claim_key] = claim.model_copy(deep=True)
                added_claims += 1
            else:
                provenance = {
                    (item.evidence_id, item.locator): item
                    for item in [*prior_claim.provenance, *claim.provenance]
                }
                stored_claims[claim.claim_key] = prior_claim.model_copy(
                    update={"provenance": list(provenance.values())}
                )
        return added_entities, added_claims

    def snapshot(
        self, tenant_id: str, task_id: str
    ) -> tuple[list[GraphEntity], list[GraphClaim]]:
        entities, claims = self._graph(tenant_id, task_id)
        return list(entities.values()), list(claims.values())


class Neo4jKnowledgeGraphStore:
    """Small HTTP repository; Neo4j remains a disposable Evidence-derived index."""

    def __init__(
        self, url: str, username: str, password: str, database: str = "neo4j"
    ) -> None:
        self.endpoint = f"{url.rstrip('/')}/db/{database}/tx/commit"
        self.auth = (username, password)
        self._schema_ready = False
        self._schema_lock = Lock()

    def _run(self, statement: str, parameters: dict[str, Any]) -> list[dict[str, Any]]:
        response = httpx.post(
            self.endpoint,
            auth=self.auth,
            json={"statements": [{"statement": statement, "parameters": parameters}]},
            timeout=30.0,
        )
        response.raise_for_status()
        payload = response.json()
        if payload.get("errors"):
            raise RuntimeError(str(payload["errors"][0].get("message")))
        results = payload.get("results") or []
        if not results:
            return []
        return [dict(item["row"][0]) for item in results[0].get("data") or []]

    def clear(self, tenant_id: str, task_id: str) -> None:
        self._run(
            "MATCH (n {tenant_id:$tenant_id, task_id:$task_id}) DETACH DELETE n RETURN {ok:true}",
            {"tenant_id": tenant_id, "task_id": task_id},
        )

    def processed_evidence_ids(self, tenant_id: str, task_id: str) -> set[str]:
        _, claims = self.snapshot(tenant_id, task_id)
        return {
            provenance.evidence_id
            for claim in claims
            for provenance in claim.provenance
        }

    def upsert(
        self,
        tenant_id: str,
        task_id: str,
        entities: list[GraphEntity],
        claims: list[GraphClaim],
    ) -> tuple[int, int]:
        self._ensure_schema()
        before_entities, before_claims = self.snapshot(tenant_id, task_id)
        before_entity_ids = {item.entity_id for item in before_entities}
        before_claim_map = {item.claim_key: item for item in before_claims}
        for entity in entities:
            self._run(
                "MERGE (e:TaskEntity {tenant_id:$tenant_id, task_id:$task_id, entity_id:$entity_id}) "
                "SET e.canonical_name=$canonical_name, e.entity_type=$entity_type, "
                "e.aliases=$aliases RETURN {ok:true}",
                {"tenant_id": tenant_id, "task_id": task_id, **entity.model_dump(mode="json")},
            )
        for claim in claims:
            params = {"tenant_id": tenant_id, "task_id": task_id, **claim.model_dump(mode="json")}
            params["qualifiers_json"] = canonical_json(params.pop("qualifiers")).decode()
            prior = before_claim_map.get(claim.claim_key)
            provenance = {
                (item.evidence_id, item.locator): item.model_dump(mode="json")
                for item in [*(prior.provenance if prior else []), *claim.provenance]
            }
            params.pop("provenance")
            params["provenance_json"] = json.dumps(
                list(provenance.values()), ensure_ascii=False, sort_keys=True
            )
            self._run(
                "MERGE (c:TaskClaim {tenant_id:$tenant_id, task_id:$task_id, claim_key:$claim_key}) "
                "SET c.claim_id=$claim_id, c.predicate=$predicate, c.subject_entity_id=$subject_entity_id, "
                "c.object_entity_id=$object_entity_id, c.value=$value, c.qualifiers_json=$qualifiers_json, "
                "c.provenance_json=$provenance_json RETURN {ok:true}",
                params,
            )
            if claim.subject_entity_id:
                self._run(
                    "MATCH (e:TaskEntity {tenant_id:$tenant_id, task_id:$task_id, entity_id:$entity_id}), "
                    "(c:TaskClaim {tenant_id:$tenant_id, task_id:$task_id, claim_key:$claim_key}) "
                    "MERGE (e)-[:SUBJECT_OF]->(c) RETURN {ok:true}",
                    {
                        "tenant_id": tenant_id,
                        "task_id": task_id,
                        "entity_id": claim.subject_entity_id,
                        "claim_key": claim.claim_key,
                    },
                )
            if claim.object_entity_id:
                self._run(
                    "MATCH (c:TaskClaim {tenant_id:$tenant_id, task_id:$task_id, claim_key:$claim_key}), "
                    "(e:TaskEntity {tenant_id:$tenant_id, task_id:$task_id, entity_id:$entity_id}) "
                    "MERGE (c)-[:OBJECT]->(e) RETURN {ok:true}",
                    {
                        "tenant_id": tenant_id,
                        "task_id": task_id,
                        "entity_id": claim.object_entity_id,
                        "claim_key": claim.claim_key,
                    },
                )
        return (
            len({item.entity_id for item in entities} - before_entity_ids),
            len({item.claim_key for item in claims} - set(before_claim_map)),
        )

    def _ensure_schema(self) -> None:
        if self._schema_ready:
            return
        with self._schema_lock:
            if self._schema_ready:
                return
            self._run(
                "CREATE CONSTRAINT task_entity_identity IF NOT EXISTS "
                "FOR (e:TaskEntity) REQUIRE (e.tenant_id, e.task_id, e.entity_id) IS UNIQUE",
                {},
            )
            self._run(
                "CREATE CONSTRAINT task_claim_identity IF NOT EXISTS "
                "FOR (c:TaskClaim) REQUIRE (c.tenant_id, c.task_id, c.claim_key) IS UNIQUE",
                {},
            )
            self._schema_ready = True

    def snapshot(
        self, tenant_id: str, task_id: str
    ) -> tuple[list[GraphEntity], list[GraphClaim]]:
        entities = self._run(
            "MATCH (e:TaskEntity {tenant_id:$tenant_id, task_id:$task_id}) "
            "RETURN {entity_id:e.entity_id, canonical_name:e.canonical_name, "
            "entity_type:e.entity_type, aliases:coalesce(e.aliases, [])}",
            {"tenant_id": tenant_id, "task_id": task_id},
        )
        claims = self._run(
            "MATCH (c:TaskClaim {tenant_id:$tenant_id, task_id:$task_id}) "
            "RETURN {claim_id:c.claim_id, claim_key:c.claim_key, predicate:c.predicate, "
            "subject_entity_id:c.subject_entity_id, object_entity_id:c.object_entity_id, "
            "value:c.value, qualifiers_json:c.qualifiers_json, provenance_json:c.provenance_json}",
            {"tenant_id": tenant_id, "task_id": task_id},
        )
        return (
            [GraphEntity.model_validate(item) for item in entities],
            [self._claim_from_neo4j(item) for item in claims],
        )

    @staticmethod
    def _claim_from_neo4j(item: dict[str, Any]) -> GraphClaim:
        payload = dict(item)
        payload["qualifiers"] = json.loads(payload.pop("qualifiers_json") or "{}")
        payload["provenance"] = json.loads(payload.pop("provenance_json") or "[]")
        return GraphClaim.model_validate(payload)


class EvidenceContentResolver:
    def __init__(self, workspace: object, documents: object) -> None:
        self.workspace = workspace
        self.documents = documents

    def resolve(self, tenant_id: str, task_id: str, evidence: Evidence) -> str:
        if evidence.data is not None:
            return canonical_json(evidence.data).decode()
        if evidence.workspace_path:
            return self.workspace.read(  # type: ignore[attr-defined]
                tenant_id, task_id, evidence.workspace_path
            ).decode("utf-8", errors="replace")
        if evidence.doc_id and evidence.chunk_id:
            return str(
                self.documents.read_chunk(  # type: ignore[attr-defined]
                    tenant_id, evidence.doc_id, evidence.chunk_id
                ).text
            )
        return f"{evidence.summary}\n{evidence.excerpt}"


class _KnowledgeGraphCancellationToken:
    def __init__(self, session_factory: SessionFactory, task_id: str) -> None:
        self.session_factory = session_factory
        self.task_id = task_id

    def is_cancelled(self) -> bool:
        with self.session_factory() as session:
            status = session.scalar(
                select(ResearchTaskRow.status).where(
                    ResearchTaskRow.task_id == self.task_id
                )
            )
            return status in {"cancelling", "cancelled"}


class TaskKnowledgeGraphService:
    def __init__(
        self,
        session_factory: SessionFactory,
        store: KnowledgeGraphStore,
        extractor: KnowledgeExtractor,
        content_resolver: EvidenceContentResolver,
    ) -> None:
        self.session_factory = session_factory
        self.store = store
        self.extractor = extractor
        self.content_resolver = content_resolver

    def build(
        self, task_id: str, evidence_ids: list[str] | None = None
    ) -> KnowledgeGraphBuildResult:
        with self.session_factory() as session:
            task = session.scalar(
                select(ResearchTaskRow).where(ResearchTaskRow.task_id == task_id)
            )
            if task is None or task.deleted_at is not None:
                raise NotFoundError("Task was not found.")
            state = ResearchState.model_validate(task.latest_state)
            bindings = ModelBindingSnapshot.model_validate(task.model_binding_snapshot)
            tenant_id = task.tenant_id
        query_hash, plan_hash = _question_hashes(state)
        evidence_by_id = {item.evidence_id: item for item in state.evidence_pool}
        requested = set(evidence_ids or evidence_by_id)
        invalid = requested - set(evidence_by_id)
        if invalid:
            raise InvalidArgumentError(
                "invalid_evidence_ids: " + ", ".join(sorted(invalid))
            )

        with self.session_factory() as session, session.begin():
            metadata = session.get(TaskKnowledgeGraphRow, (tenant_id, task_id))
            if metadata is None:
                metadata = TaskKnowledgeGraphRow(
                    tenant_id=tenant_id,
                    task_id=task_id,
                    status="building",
                    query_hash=query_hash,
                    plan_hash=plan_hash,
                    processed_evidence_count=0,
                    updated_at=utcnow(),
                )
                session.add(metadata)
            elif metadata.query_hash != query_hash or metadata.plan_hash != plan_hash:
                metadata.query_hash = query_hash
                metadata.plan_hash = plan_hash
                metadata.processed_evidence_count = 0
                metadata.status = "building"
            rebuild = metadata.processed_evidence_count == 0 and metadata.status == "building"
        if rebuild:
            self.store.clear(tenant_id, task_id)

        processed = self.store.processed_evidence_ids(tenant_id, task_id)
        selected = [evidence_by_id[item] for item in sorted(requested - processed)]
        node_run_id: str | None = None
        input_checkpoint_id: str | None = None
        if selected:
            node_run_id, input_checkpoint_id = self._begin_build_run(
                tenant_id=tenant_id,
                task_id=task_id,
                query_hash=query_hash,
                plan_hash=plan_hash,
                evidence_ids=[item.evidence_id for item in selected],
            )
        execution = ExecutionContext(
            tenant_id=tenant_id,
            user_id=task.user_id,
            task_id=task_id,
            thread_id=task.thread_id,
            lease_token=f"kg:{node_run_id or 'noop'}",
            cancellation_token=_KnowledgeGraphCancellationToken(
                self.session_factory, task_id
            ),
            deadline=utcnow() + timedelta(minutes=10),
            capability_snapshot=dict(task.capability_snapshot),
        )
        hypotheses = (
            [
                {"hypothesis_id": item.hypothesis_id, "claim": item.claim}
                for item in state.plan.hypotheses
            ]
            if state.plan
            else []
        )
        staged_entities: list[GraphEntity] = []
        staged_claims: list[GraphClaim] = []
        skipped: list[str] = []
        ungrounded = 0
        effect_context = (
            bind_runtime_effect_context(execution, node_run_id)
            if node_run_id is not None
            else nullcontext()
        )
        with effect_context:
            for evidence in selected:
                try:
                    extracted = self.extractor.extract(
                        tenant_id=tenant_id,
                        bindings=bindings,
                        query=state.query,
                        hypotheses=hypotheses,
                        evidence=evidence,
                        content=self.content_resolver.resolve(
                            tenant_id, task_id, evidence
                        ),
                    )
                    entities, claims, dropped = self._normalize_extraction(
                        tenant_id, task_id, evidence, extracted
                    )
                    staged_entities.extend(entities)
                    staged_claims.extend(claims)
                    ungrounded += dropped
                except Exception:
                    skipped.append(evidence.evidence_id)

        with self.session_factory() as session:
            current = session.scalar(
                select(ResearchTaskRow).where(ResearchTaskRow.task_id == task_id)
            )
            if current is None:
                raise NotFoundError("Task was not found.")
            current_state = ResearchState.model_validate(current.latest_state)
        if _question_hashes(current_state) != (query_hash, plan_hash):
            self._mark_stale(tenant_id, task_id, current_state)
            result = KnowledgeGraphBuildResult(
                status="partial",
                entity_count=0,
                claim_count=0,
                relation_count=0,
                processed_evidence_count=len(processed),
                stale_discarded=True,
            )
            self._complete_build_run(node_run_id, input_checkpoint_id, result)
            return result

        staged_entities, staged_claims = self._coalesce(
            staged_entities, staged_claims
        )
        added_entities, added_claims = self.store.upsert(
            tenant_id, task_id, staged_entities, staged_claims
        )
        entities, claims = self.store.snapshot(tenant_id, task_id)
        status: Literal["ready", "partial"] = "partial" if skipped else "ready"
        processed_ids = {
            item.evidence_id for claim in claims for item in claim.provenance
        }
        conflict_groups: dict[tuple[str | None, str, str], set[tuple[str | None, str | None]]] = {}
        for claim in claims:
            group = (
                claim.subject_entity_id,
                _normalized(claim.predicate),
                canonical_json(claim.qualifiers).decode(),
            )
            conflict_groups.setdefault(group, set()).add(
                (claim.object_entity_id, claim.value)
            )
        conflicts_kept = sum(max(0, len(values) - 1) for values in conflict_groups.values())
        with self.session_factory() as session, session.begin():
            metadata = session.get(TaskKnowledgeGraphRow, (tenant_id, task_id))
            assert metadata is not None
            metadata.status = status
            metadata.processed_evidence_count = len(processed_ids)
            metadata.updated_at = utcnow()
        result = KnowledgeGraphBuildResult(
            status=status,
            entity_count=len(entities),
            claim_count=len(claims),
            relation_count=sum(1 for item in claims if item.subject_entity_id),
            processed_evidence_count=len(processed_ids),
            skipped_evidence_ids=skipped,
            added={
                "entities": added_entities,
                "claims": added_claims,
                "relations": added_claims,
            },
            conflicts_kept=conflicts_kept,
            ungrounded_dropped=ungrounded,
        )
        self._complete_build_run(node_run_id, input_checkpoint_id, result)
        return result

    def _begin_build_run(
        self,
        *,
        tenant_id: str,
        task_id: str,
        query_hash: str,
        plan_hash: str,
        evidence_ids: list[str],
    ) -> tuple[str, str]:
        evidence_hash = hashlib.sha256(canonical_json(sorted(evidence_ids))).hexdigest()
        node_key = (
            f"task_kg:{query_hash[:16]}:{plan_hash[:16]}:{evidence_hash[:24]}"
        )
        with self.session_factory() as session, session.begin():
            checkpoint = session.scalar(
                select(ResearchCheckpointRow)
                .where(ResearchCheckpointRow.task_id == task_id)
                .order_by(ResearchCheckpointRow.checkpoint_seq.desc())
                .limit(1)
            )
            if checkpoint is None:
                raise RuntimeError("Task has no checkpoint for Task KG build")
            node_run = session.scalar(
                select(NodeRunRow)
                .where(
                    NodeRunRow.task_id == task_id,
                    NodeRunRow.node_key == node_key,
                    NodeRunRow.input_checkpoint_id == checkpoint.checkpoint_id,
                )
                .with_for_update()
            )
            if node_run is None:
                node_run = NodeRunRow(
                    node_run_id=new_id("node"),
                    tenant_id=tenant_id,
                    task_id=task_id,
                    node_key=node_key,
                    node_name="knowledge_graph_build",
                    input_checkpoint_id=checkpoint.checkpoint_id,
                    status="pending",
                    attempt_count=0,
                )
                session.add(node_run)
                session.flush()
            if node_run.status != "completed":
                node_run.status = "running"
                node_run.attempt_count += 1
                node_run.updated_at = utcnow()
            return node_run.node_run_id, checkpoint.checkpoint_id

    def _complete_build_run(
        self,
        node_run_id: str | None,
        input_checkpoint_id: str | None,
        result: KnowledgeGraphBuildResult,
    ) -> None:
        if node_run_id is None or input_checkpoint_id is None:
            return
        with self.session_factory() as session, session.begin():
            node_run = session.get(NodeRunRow, node_run_id)
            if node_run is None:
                return
            node_run.status = "completed"
            node_run.output_checkpoint_id = input_checkpoint_id
            node_run.result_json = NodeResult(
                output={"knowledge_graph": result.model_dump(mode="json")}
            ).model_dump(mode="json")
            node_run.updated_at = utcnow()

    @staticmethod
    def _coalesce(
        entities: list[GraphEntity], claims: list[GraphClaim]
    ) -> tuple[list[GraphEntity], list[GraphClaim]]:
        entity_map: dict[str, GraphEntity] = {}
        for entity in entities:
            prior_entity = entity_map.get(entity.entity_id)
            entity_map[entity.entity_id] = (
                entity
                if prior_entity is None
                else prior_entity.model_copy(
                    update={
                        "aliases": sorted(
                            set(prior_entity.aliases) | set(entity.aliases)
                        )
                    }
                )
            )
        claim_map: dict[str, GraphClaim] = {}
        for claim in claims:
            prior_claim = claim_map.get(claim.claim_key)
            if prior_claim is None:
                claim_map[claim.claim_key] = claim
                continue
            provenance = {
                (item.evidence_id, item.locator): item
                for item in [*prior_claim.provenance, *claim.provenance]
            }
            claim_map[claim.claim_key] = prior_claim.model_copy(
                update={"provenance": list(provenance.values())}
            )
        return list(entity_map.values()), list(claim_map.values())

    def _mark_stale(
        self, tenant_id: str, task_id: str, state: ResearchState
    ) -> None:
        query_hash, plan_hash = _question_hashes(state)
        now = utcnow()
        with self.session_factory() as session, session.begin():
            metadata = session.get(TaskKnowledgeGraphRow, (tenant_id, task_id))
            if metadata is None:
                metadata = TaskKnowledgeGraphRow(
                    tenant_id=tenant_id,
                    task_id=task_id,
                    status="building",
                    query_hash=query_hash,
                    plan_hash=plan_hash,
                    processed_evidence_count=0,
                    updated_at=now,
                )
                session.add(metadata)
            else:
                metadata.status = "building"
                metadata.query_hash = query_hash
                metadata.plan_hash = plan_hash
                metadata.processed_evidence_count = 0
                metadata.updated_at = now
            session.add(
                OutboxMessageRow(
                    message_id=new_id("outbox"),
                    tenant_id=tenant_id,
                    kind="knowledge_graph.build",
                    aggregate_id=task_id,
                    payload_json={"task_id": task_id},
                    idempotency_key=f"kg:stale:{task_id}:{query_hash}:{plan_hash}",
                    available_at=now,
                )
            )

    @staticmethod
    def _normalize_extraction(
        tenant_id: str,
        task_id: str,
        evidence: Evidence,
        extracted: ExtractedGraph,
    ) -> tuple[list[GraphEntity], list[GraphClaim], int]:
        namespace = f"{tenant_id}|{task_id}"
        aliases: dict[str, ExtractedEntity] = {}
        entities: dict[str, GraphEntity] = {}
        for raw_entity in extracted.entities:
            entity_type = (
                raw_entity.entity_type
                if raw_entity.entity_type in ENTITY_TYPES
                else "Concept"
            )
            normalized_name = _normalized(raw_entity.canonical_name)
            entity_id = _stable_id("entity", namespace, normalized_name, entity_type)
            entity = GraphEntity(
                entity_id=entity_id,
                canonical_name=raw_entity.canonical_name.strip(),
                entity_type=entity_type,
                aliases=sorted(set(raw_entity.aliases)),
            )
            entities[entity_id] = entity
            for name in [raw_entity.canonical_name, *raw_entity.aliases]:
                aliases[_normalized(name)] = raw_entity

        def resolve(name: str | None) -> str | None:
            if not name:
                return None
            raw = aliases.get(_normalized(name)) or ExtractedEntity(canonical_name=name)
            entity_type = raw.entity_type if raw.entity_type in ENTITY_TYPES else "Concept"
            entity_id = _stable_id("entity", namespace, _normalized(raw.canonical_name), entity_type)
            entities.setdefault(
                entity_id,
                GraphEntity(
                    entity_id=entity_id,
                    canonical_name=raw.canonical_name.strip(),
                    entity_type=entity_type,
                    aliases=sorted(set(raw.aliases)),
                ),
            )
            return entity_id

        claims: list[GraphClaim] = []
        dropped = 0
        for raw_claim in extracted.claims:
            if raw_claim.evidence_id != evidence.evidence_id:
                dropped += 1
                continue
            subject_id = resolve(raw_claim.subject)
            object_id = resolve(raw_claim.object)
            normalized_qualifiers = {
                _normalized(key): _normalized(value)
                for key, value in sorted(raw_claim.qualifiers.items())
            }
            claim_key = hashlib.sha256(
                canonical_json(
                    {
                        "task_id": task_id,
                        "subject": _normalized(raw_claim.subject or ""),
                        "predicate": _normalized(raw_claim.predicate),
                        "object": _normalized(raw_claim.object or ""),
                        "value": _normalized(raw_claim.value or ""),
                        "qualifiers": normalized_qualifiers,
                    }
                )
            ).hexdigest()
            claims.append(
                GraphClaim(
                    claim_id=_stable_id("claim", namespace, claim_key),
                    claim_key=claim_key,
                    predicate=raw_claim.predicate.strip(),
                    subject_entity_id=subject_id,
                    object_entity_id=object_id,
                    value=raw_claim.value,
                    qualifiers=raw_claim.qualifiers,
                    provenance=[
                        GraphProvenance(
                            evidence_id=evidence.evidence_id,
                            locator=raw_claim.locator,
                        )
                    ],
                )
            )
        return list(entities.values()), claims, dropped

    def query(
        self,
        tenant_id: str,
        user_id: str,
        task_id: str,
        request: KnowledgeGraphQuery,
    ) -> KnowledgeGraphQueryResult:
        with self.session_factory() as session:
            task = session.scalar(
                select(ResearchTaskRow).where(
                    ResearchTaskRow.tenant_id == tenant_id,
                    ResearchTaskRow.user_id == user_id,
                    ResearchTaskRow.task_id == task_id,
                    ResearchTaskRow.deleted_at.is_(None),
                )
            )
            if task is None:
                raise NotFoundError("Task was not found.")
            metadata = session.get(TaskKnowledgeGraphRow, (tenant_id, task_id))
        status = cast(
            Literal["building", "ready", "partial"],
            metadata.status if metadata else "building",
        )
        if status == "building":
            return KnowledgeGraphQueryResult(graph_status="building")
        entities, claims = self.store.snapshot(tenant_id, task_id)
        entity_map = {item.entity_id: item for item in entities}

        if request.op == "search":
            needle = _normalized(request.query or "")
            matched_entities = [
                item
                for item in entities
                if needle in _normalized(item.canonical_name)
                or any(needle in _normalized(alias) for alias in item.aliases)
            ]
            matched_claims = [
                item
                for item in claims
                if needle in _normalized(item.predicate)
                or needle in _normalized(item.value or "")
            ]
            return self._result(status, matched_entities, matched_claims, request.limit)

        source_matches = self._resolve(entities, request.entity or "")
        if len(source_matches) != 1:
            return KnowledgeGraphQueryResult(
                graph_status=status,
                resolution_status="miss" if not source_matches else "ambiguous",
                candidates=source_matches[: request.limit],
            )
        source = source_matches[0]
        if request.op == "neighbors":
            matched_claims = [
                item
                for item in claims
                if source.entity_id in {item.subject_entity_id, item.object_entity_id}
                and (
                    request.relation is None
                    or _normalized(item.predicate) == _normalized(request.relation)
                )
            ]
            involved = {source.entity_id}
            for claim in matched_claims:
                involved.update(
                    item
                    for item in [claim.subject_entity_id, claim.object_entity_id]
                    if item
                )
            result = self._result(
                status,
                [entity_map[item] for item in involved if item in entity_map],
                matched_claims,
                request.limit,
            )
            result.resolution_status = "hit"
            return result

        target_matches = self._resolve(entities, request.target_entity or "")
        if len(target_matches) != 1:
            return KnowledgeGraphQueryResult(
                graph_status=status,
                resolution_status="miss" if not target_matches else "ambiguous",
                candidates=target_matches[: request.limit],
            )
        path = self._path(
            source.entity_id,
            target_matches[0].entity_id,
            claims,
            request.max_hops,
            request.relation,
        )
        if path is None:
            return KnowledgeGraphQueryResult(
                graph_status=status, resolution_status="miss"
            )
        path_claims = [
            claim for claim in claims if claim.claim_id in set(path.claim_ids)
        ]
        result = self._result(
            status,
            [entity_map[item] for item in path.entity_ids if item in entity_map],
            path_claims,
            request.limit,
        )
        result.resolution_status = "hit"
        result.paths = [path]
        return result

    @staticmethod
    def _resolve(entities: list[GraphEntity], value: str) -> list[GraphEntity]:
        normalized = _normalized(value)
        return [
            item
            for item in entities
            if item.entity_id == value
            or _normalized(item.canonical_name) == normalized
            or normalized in {_normalized(alias) for alias in item.aliases}
        ]

    @staticmethod
    def _result(
        status: Literal["building", "ready", "partial"],
        entities: list[GraphEntity],
        claims: list[GraphClaim],
        limit: int,
    ) -> KnowledgeGraphQueryResult:
        truncated = len(entities) > limit or len(claims) > limit
        bounded_claims = claims[:limit]
        evidence_ids = sorted(
            {
                item.evidence_id
                for claim in bounded_claims
                for item in claim.provenance
            }
        )
        return KnowledgeGraphQueryResult(
            graph_status=status,
            resolution_status="hit" if entities or claims else "miss",
            entities=entities[:limit],
            claims=bounded_claims,
            relations=[
                GraphRelation(
                    from_entity_id=claim.subject_entity_id,
                    to_entity_id=claim.object_entity_id,
                    predicate=claim.predicate,
                    claim_id=claim.claim_id,
                )
                for claim in bounded_claims
                if claim.subject_entity_id
            ],
            evidence_ids=evidence_ids,
            truncated=truncated,
        )

    @staticmethod
    def _path(
        source: str,
        target: str,
        claims: list[GraphClaim],
        max_hops: int,
        relation: str | None,
    ) -> GraphPath | None:
        adjacency: dict[str, list[tuple[str, str]]] = {}
        for claim in claims:
            if not claim.subject_entity_id or not claim.object_entity_id:
                continue
            if relation and _normalized(claim.predicate) != _normalized(relation):
                continue
            adjacency.setdefault(claim.subject_entity_id, []).append(
                (claim.object_entity_id, claim.claim_id)
            )
            adjacency.setdefault(claim.object_entity_id, []).append(
                (claim.subject_entity_id, claim.claim_id)
            )
        queue: deque[tuple[str, list[str], list[str]]] = deque(
            [(source, [source], [])]
        )
        visited = {source}
        while queue:
            current, entity_path, claim_path = queue.popleft()
            if current == target:
                return GraphPath(
                    entity_ids=entity_path,
                    claim_ids=claim_path,
                    hops=len(claim_path),
                )
            if len(claim_path) >= max_hops:
                continue
            for neighbor, claim_id in adjacency.get(current, []):
                if neighbor not in visited:
                    visited.add(neighbor)
                    queue.append(
                        (neighbor, [*entity_path, neighbor], [*claim_path, claim_id])
                    )
        return None


class KnowledgeGraphToolAdapter(ToolAdapter):
    def __init__(self, service: TaskKnowledgeGraphService) -> None:
        self.service = service

    def execute(
        self, context: ToolExecutionContext, normalized_arguments: dict[str, Any]
    ) -> ToolResult:
        try:
            result = self.service.query(
                context.tenant_id,
                context.user_id,
                context.task_id,
                KnowledgeGraphQuery.model_validate(normalized_arguments),
            )
            return ToolResult(
                status="success",
                summary=f"Task knowledge graph is {result.graph_status}.",
                data=result.model_dump(mode="json"),
            )
        except InvalidArgumentError as exc:
            return ToolResult(
                status="error",
                error=ToolError(
                    type="invalid_argument", code=exc.code, message=exc.detail
                ),
            )
        except NotFoundError as exc:
            return ToolResult(
                status="error",
                error=ToolError(type="not_found", code=exc.code, message=exc.detail),
            )


def tool_definitions(service: TaskKnowledgeGraphService) -> list[ToolDefinition]:
    return [
        ToolDefinition(
            name="query_knowledge_graph",
            args_schema=KnowledgeGraphQuery,
            adapter=KnowledgeGraphToolAdapter(service),
            evidence_eligible=False,
            provider_ref="local:task-knowledge-graph:v1",
        )
    ]
