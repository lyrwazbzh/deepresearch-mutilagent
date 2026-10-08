from __future__ import annotations

import hashlib
import json
import os
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from src.agent.tools.documents import IngestionService
from src.agent.config import Settings
from src.agent.container import build_container
from src.agent.schema.model_config import ModelBindingSnapshot
from src.agent.schema.state import Evidence
from src.agent.tools.knowledge_graph import (
    ExtractedClaim,
    ExtractedEntity,
    ExtractedGraph,
    KnowledgeGraphQuery,
)
from src.agent.backends.persistence.models import ResearchTaskRow, ShareRow, TaskKnowledgeGraphRow


class DeterministicRecoveryVectors:
    def __init__(self, dimensions: int) -> None:
        self.dimensions = dimensions

    def embed(self, _tenant_id: str, _binding: Any, texts: list[str]) -> list[list[float]]:
        vectors: list[list[float]] = []
        for text in texts:
            digest = hashlib.sha256(text.encode()).digest()
            vectors.append(
                [
                    ((digest[index % len(digest)] / 255.0) * 2.0) - 1.0
                    for index in range(self.dimensions)
                ]
            )
        return vectors

    def rerank(
        self,
        _tenant_id: str,
        _binding: Any,
        _query: str,
        candidates: list[str],
    ) -> list[float]:
        return [1.0 / (index + 1) for index, _candidate in enumerate(candidates)]


class DeterministicRecoveryExtractor:
    """Fixed recovery parser used only to prove Evidence -> Neo4j rebuildability."""

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
        del tenant_id, bindings, query, hypotheses, content
        return ExtractedGraph(
            entities=[
                ExtractedEntity(
                    canonical_name="Delivery Chain",
                    entity_type="Concept",
                )
            ],
            claims=[
                ExtractedClaim(
                    predicate="is_grounded_by",
                    subject="Delivery Chain",
                    value=evidence.summary,
                    evidence_id=evidence.evidence_id,
                    locator="recovery fixture",
                )
            ],
        )


def _embedding(container: Any, tenant_id: str) -> Any:
    with container.session_factory() as session:
        return container.model_resolver.resolve_snapshot(
            session, tenant_id, ("embedding",)
        ).get("embedding")


def seed(container: Any, output: Path) -> None:
    tenant = container.settings.default_tenant_id
    doc_id = container.document_service.create(
        tenant_id=tenant,
        name=f"phase14-recovery-{time.time_ns()}.txt",
        media_type="text/plain",
        data=(
            b"Phase 14 recovery fixture. Northstar revenue was USD 42 million "
            b"in fiscal year 2026."
        ),
        tags=["phase14-recovery"],
    )
    generation = IngestionService(
        container.session_factory,
        container.document_service,
        DeterministicRecoveryVectors(container.settings.openai_embedding_dimensions),
        _embedding(container, tenant),
    ).run(tenant, doc_id)
    share_secret = json.loads(Path(os.environ["PHASE14_SHARE_SECRET_FILE"]).read_text())
    with container.session_factory() as session:
        share = session.get(ShareRow, share_secret["share_id"])
        if share is None:
            raise RuntimeError("Share recovery fixture is missing")
        task = session.get(ResearchTaskRow, share.task_id)
        if task is None or not task.latest_state.get("evidence_pool"):
            raise RuntimeError("KG recovery fixture has no Evidence")
        task_id = task.task_id
    container.knowledge_graph_service.extractor = DeterministicRecoveryExtractor()
    kg = container.knowledge_graph_service.build(task_id)
    query = container.knowledge_graph_service.query(
        tenant,
        container.settings.default_user_id,
        task_id,
        KnowledgeGraphQuery(op="neighbors", entity="Delivery Chain", limit=20),
    )
    result = {
        "tenant_id": tenant,
        "doc_id": doc_id,
        "document_generation": generation,
        "kg_task_id": task_id,
        "kg_processed_evidence_count": kg.processed_evidence_count,
        "kg_claims": len(query.claims),
    }
    output.write_text(json.dumps(result), encoding="utf-8")
    output.chmod(0o600)
    print(json.dumps({"mode": "seed", **result}, sort_keys=True))


def rebuild(container: Any, fixture: dict[str, object]) -> None:
    tenant = str(fixture["tenant_id"])
    doc_id = str(fixture["doc_id"])
    generation = container.document_service.queue_rebuild(tenant, doc_id)
    rebuilt_generation = IngestionService(
        container.session_factory,
        container.document_service,
        DeterministicRecoveryVectors(container.settings.openai_embedding_dimensions),
        _embedding(container, tenant),
    ).run(tenant, doc_id)
    hits = container.document_service.index.bm25(
        "Northstar revenue 2026",
        tenant_id=tenant,
        generations=container.document_service.generations(tenant),
        doc_ids={doc_id},
        tags=None,
        limit=5,
    )
    task_id = str(fixture["kg_task_id"])
    container.knowledge_graph_service.store.clear(tenant, task_id)
    with container.session_factory() as session, session.begin():
        metadata = session.get(TaskKnowledgeGraphRow, (tenant, task_id))
        if metadata is None:
            raise RuntimeError("restored Task KG lifecycle metadata is missing")
        metadata.status = "building"
        metadata.processed_evidence_count = 0
        metadata.updated_at = datetime.now(UTC)
    container.knowledge_graph_service.extractor = DeterministicRecoveryExtractor()
    kg = container.knowledge_graph_service.build(task_id)
    query = container.knowledge_graph_service.query(
        tenant,
        container.settings.default_user_id,
        task_id,
        KnowledgeGraphQuery(op="neighbors", entity="Delivery Chain", limit=20),
    )
    if rebuilt_generation != generation or not hits or not query.claims:
        raise RuntimeError("Elasticsearch or Task KG rebuild did not restore queryability")
    print(
        json.dumps(
            {
                "mode": "rebuild",
                "doc_id": doc_id,
                "document_generation": generation,
                "search_hits": len(hits),
                "kg_task_id": task_id,
                "kg_processed_evidence_count": kg.processed_evidence_count,
                "kg_claims": len(query.claims),
            },
            sort_keys=True,
        )
    )


def main() -> None:
    settings = Settings.from_env()
    container = build_container(settings)
    fixture_path = Path(os.environ["PHASE14_RECOVERY_FIXTURE_OUTPUT"])
    mode = os.environ["PHASE14_RECOVERY_FIXTURE_MODE"]
    if mode == "seed":
        seed(container, fixture_path)
    elif mode == "rebuild":
        rebuild(container, json.loads(fixture_path.read_text()))
    else:
        raise RuntimeError(f"unsupported recovery fixture mode: {mode}")


if __name__ == "__main__":
    main()
