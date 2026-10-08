from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import select

from src.agent.config import Settings
from src.agent.container import build_container
from src.agent.schema.capability import CapabilitySnapshot
from src.agent.schema.state import Evidence
from src.agent.backends.events import InMemoryEventBroker
from src.agent.tools.knowledge_graph import (
    EvidenceContentResolver,
    ExtractedClaim,
    ExtractedEntity,
    ExtractedGraph,
    GraphEntity,
    InMemoryKnowledgeGraphStore,
    KnowledgeGraphQuery,
    TaskKnowledgeGraphService,
)
from src.agent.backends.persistence.models import NodeRunRow, ResearchTaskRow, TaskKnowledgeGraphRow
from src.agent.llm.runtime import FakeModelRuntime
from src.agent.errors import InvalidArgumentError, NotFoundError
from src.agent.task_service import CreateTaskRequest


class GroundedExtractor:
    def extract(self, **kwargs: Any) -> ExtractedGraph:
        evidence: Evidence = kwargs["evidence"]
        value = "12" if evidence.evidence_id == "ev_conflict" else "10"
        return ExtractedGraph(
            entities=[
                ExtractedEntity(
                    canonical_name="Acme", entity_type="Organization", aliases=["ACME Corp"]
                ),
                ExtractedEntity(canonical_name="Enterprise Market", entity_type="Concept"),
            ],
            claims=[
                ExtractedClaim(
                    subject="Acme",
                    predicate="annual revenue",
                    value=value,
                    qualifiers={"year": "2025", "unit": "USD million"},
                    evidence_id=evidence.evidence_id,
                    locator=f"row:{evidence.evidence_id}",
                ),
                ExtractedClaim(
                    subject="Acme",
                    predicate="operates in",
                    object="Enterprise Market",
                    evidence_id=evidence.evidence_id,
                ),
                ExtractedClaim(
                    subject="Acme",
                    predicate="must be dropped",
                    value="unsupported",
                    evidence_id="ev_not_current",
                ),
            ],
        )


def _evidence(evidence_id: str) -> Evidence:
    return Evidence(
        evidence_id=evidence_id,
        hypothesis_id="h1",
        summary="Acme revenue",
        excerpt="Revenue was reported.",
        source_type="sql",
        stance="support",
        relevance=1,
        data={"company": "Acme", "revenue": 10},
        content_hash=(evidence_id[-1:] or "a") * 64,
    )


def test_task_kg_incremental_identity_provenance_conflicts_and_query(
    make_container, session_factory
) -> None:
    container, _ = make_container({})
    task_id = container.task_service.create(
        "tenant_test",
        "user_test",
        "phase9-kg",
        CreateTaskRequest(query="Compare Acme revenue"),
    ).projection.task_id
    evidence = [_evidence("ev_primary"), _evidence("ev_duplicate"), _evidence("ev_conflict")]
    with session_factory() as session, session.begin():
        task = session.get(ResearchTaskRow, task_id)
        assert task is not None
        state = dict(task.latest_state)
        state["evidence_pool"] = [item.model_dump(mode="json") for item in evidence]
        task.latest_state = state

    store = InMemoryKnowledgeGraphStore()
    service = TaskKnowledgeGraphService(
        session_factory,
        store,
        GroundedExtractor(),
        EvidenceContentResolver(
            container.workspace_service, container.document_service
        ),
    )
    first = service.build(task_id)
    replay = service.build(task_id)

    assert first.status == "ready"
    assert first.entity_count == 2
    assert first.claim_count == 3
    assert first.processed_evidence_count == 3
    assert first.ungrounded_dropped == 3
    assert first.conflicts_kept == 1
    assert replay.added == {"entities": 0, "claims": 0, "relations": 0}
    entities, claims = store.snapshot("tenant_test", task_id)
    with session_factory() as session:
        build_run = session.scalar(
            select(NodeRunRow).where(
                NodeRunRow.task_id == task_id,
                NodeRunRow.node_name == "knowledge_graph_build",
            )
        )
        assert build_run is not None and build_run.status == "completed"
    duplicate = next(item for item in claims if item.value == "10")
    assert {item.evidence_id for item in duplicate.provenance} == {
        "ev_primary",
        "ev_duplicate",
    }
    assert len({item.entity_id for item in entities}) == 2
    before_query_evidence = list(evidence)

    neighbors = service.query(
        "tenant_test",
        "user_test",
        task_id,
        KnowledgeGraphQuery(op="neighbors", entity="ACME Corp"),
    )
    assert neighbors.resolution_status == "hit"
    assert len(neighbors.claims) == 3
    assert neighbors.evidence_ids == ["ev_conflict", "ev_duplicate", "ev_primary"]

    search = service.query(
        "tenant_test",
        "user_test",
        task_id,
        KnowledgeGraphQuery(op="search", query="revenue", limit=1),
    )
    assert search.truncated is True
    assert len(search.claims) == 1
    path = service.query(
        "tenant_test",
        "user_test",
        task_id,
        KnowledgeGraphQuery(
            op="path", entity="Acme", target_entity="Enterprise Market", max_hops=2
        ),
    )
    assert path.resolution_status == "hit"
    assert path.paths[0].hops == 1

    store.upsert(
        "tenant_test",
        task_id,
        [
            GraphEntity(
                entity_id="entity_ambiguous",
                canonical_name="Acme",
                entity_type="Product",
            )
        ],
        [],
    )
    ambiguous = service.query(
        "tenant_test",
        "user_test",
        task_id,
        KnowledgeGraphQuery(op="neighbors", entity="Acme"),
    )
    assert ambiguous.resolution_status == "ambiguous"
    assert len(ambiguous.candidates) == 2

    store.clear("tenant_test", task_id)
    rebuilt = service.build(task_id)
    assert rebuilt.entity_count == 2 and rebuilt.claim_count == 3
    with session_factory() as session:
        task = session.get(ResearchTaskRow, task_id)
        assert task is not None
        assert task.latest_state["evidence_pool"] == [
            item.model_dump(mode="json") for item in before_query_evidence
        ]
    with pytest.raises(NotFoundError):
        service.query(
            "tenant_other",
            "user_test",
            task_id,
            KnowledgeGraphQuery(op="search", query="revenue"),
        )


def test_task_kg_rejects_foreign_evidence_and_reports_building(
    make_container, session_factory
) -> None:
    container, _ = make_container({})
    task_id = container.task_service.create(
        "tenant_test",
        "user_test",
        "phase9-kg-invalid",
        CreateTaskRequest(query="question"),
    ).projection.task_id
    service = TaskKnowledgeGraphService(
        session_factory,
        InMemoryKnowledgeGraphStore(),
        GroundedExtractor(),
        EvidenceContentResolver(
            container.workspace_service, container.document_service
        ),
    )
    building = service.query(
        "tenant_test",
        "user_test",
        task_id,
        KnowledgeGraphQuery(op="search", query="anything"),
    )
    assert building.graph_status == "building"
    with pytest.raises(InvalidArgumentError, match="invalid_evidence_ids"):
        service.build(task_id, ["foreign_evidence"])
    with session_factory() as session:
        assert session.get(TaskKnowledgeGraphRow, ("tenant_test", task_id)) is None


def test_task_kg_discards_stale_replan_build(make_container, session_factory) -> None:
    container, _ = make_container({})
    task_id = container.task_service.create(
        "tenant_test",
        "user_test",
        "phase9-kg-stale",
        CreateTaskRequest(query="old question"),
    ).projection.task_id
    with session_factory() as session, session.begin():
        task = session.get(ResearchTaskRow, task_id)
        assert task is not None
        state = dict(task.latest_state)
        state["evidence_pool"] = [_evidence("ev_stale").model_dump(mode="json")]
        task.latest_state = state

    class ReplanningExtractor(GroundedExtractor):
        changed = False

        def extract(self, **kwargs: Any) -> ExtractedGraph:
            if not self.changed:
                with session_factory() as session, session.begin():
                    task = session.get(ResearchTaskRow, task_id)
                    assert task is not None
                    state = dict(task.latest_state)
                    state["query"] = "new question"
                    task.latest_state = state
                self.changed = True
            return super().extract(**kwargs)

    store = InMemoryKnowledgeGraphStore()
    service = TaskKnowledgeGraphService(
        session_factory,
        store,
        ReplanningExtractor(),
        EvidenceContentResolver(
            container.workspace_service, container.document_service
        ),
    )
    result = service.build(task_id)

    assert result.stale_discarded is True
    assert store.snapshot("tenant_test", task_id) == ([], [])
    with session_factory() as session:
        metadata = session.get(TaskKnowledgeGraphRow, ("tenant_test", task_id))
        assert metadata is not None and metadata.status == "building"


def test_phase9_registers_query_only_for_deep_scout(session_factory) -> None:
    store = InMemoryKnowledgeGraphStore()
    container = build_container(
        Settings(
            database_url="sqlite://",
            redis_url="redis://unused",
            celery_broker_url="redis://unused",
            default_tenant_id="tenant_test",
            default_user_id="user_test",
            model_mode="fake",
            phase9_enabled=True,
        ),
        session_factory=session_factory,
        model_runtime=FakeModelRuntime(),
        event_broker=InMemoryEventBroker(),
        knowledge_graph_store=store,
    )
    assert container.tool_registry.get("query_knowledge_graph") is not None
    task_id = container.task_service.create(
        "tenant_test",
        "user_test",
        "phase9-capability",
        CreateTaskRequest(query="question"),
    ).projection.task_id
    with session_factory() as session:
        task = session.get(ResearchTaskRow, task_id)
        assert task is not None
        snapshot = CapabilitySnapshot.model_validate(task.capability_snapshot)
    assert "query_knowledge_graph" in {item.name for item in snapshot.tools}
    assert container.tool_policy_registry.resolve(
        snapshot.policy_revision, "deep_scout"
    ).allows(
        "query_knowledge_graph"
    )
    for agent in ("data_analyst", "code_wizard"):
        assert not container.tool_policy_registry.resolve(
            snapshot.policy_revision, agent
        ).allows(
            "query_knowledge_graph"
        )
    assert container.tool_registry.get("build_knowledge_graph") is None
