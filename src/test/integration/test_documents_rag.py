from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from src.api_view.web_main import create_app
from src.agent.tools.documents import (
    ElasticsearchDocumentIndex,
    IndexedChunk,
    IngestionService,
    rrf,
)
from src.agent.schema.model_config import FrozenModelBinding, ResolvedModelRevision
from src.agent.backends.persistence.models import DocumentIngestionRow, DocumentRow


def binding(key, purpose):
    return FrozenModelBinding(
        binding_key=key,
        purpose=purpose,
        primary=ResolvedModelRevision(model_config_id=f"model_{purpose}", version=1),
    )


def test_document_generation_hybrid_retrieval_parent_expansion(make_container) -> None:
    container, runtime = make_container({})
    doc_id = container.document_service.create(
        tenant_id="tenant_test",
        name="research.txt",
        media_type="text/plain",
        data=("alpha evidence supports the hypothesis. " * 200).encode(),
        tags=["research"],
    )
    ingestion = IngestionService(
        container.session_factory,
        container.document_service,
        runtime,
        binding("embedding", "embedding"),
        parent_chars=400,
        child_chars=120,
        overlap_chars=20,
    )
    assert ingestion.run("tenant_test", doc_id) == 1
    document = container.document_service.get("tenant_test", doc_id)
    assert document.status == "ready"
    assert document.published_generation == 1

    hits, degraded = container.retrieval_gateway.search(
        tenant_id="tenant_test",
        query="alpha evidence",
        embedding_binding=binding("embedding", "embedding"),
        rerank_binding=binding("rerank", "rerank"),
        top_k=3,
    )
    assert hits and degraded == []
    assert hits[0].chunk.parent_id
    assert len(hits[0].context_text) >= len(hits[0].chunk.text)
    targeted = container.document_service.read_chunk(
        "tenant_test", doc_id, hits[0].chunk.chunk_id
    )
    assert targeted.content_hash == document.content_hash
    assert container.document_service.generations("other") == {}

    assert container.document_service.queue_rebuild("tenant_test", doc_id) == 2
    with pytest.raises(RuntimeError, match="injected"):
        ingestion.run("tenant_test", doc_id, fail_stage="indexing")
    failed = container.document_service.get("tenant_test", doc_id)
    assert failed.status == "ready"
    assert failed.published_generation == 1
    assert ingestion.run("tenant_test", doc_id) == 2
    assert container.document_service.get("tenant_test", doc_id).published_generation == 2
    assert container.document_service.read_chunk(
        "tenant_test", doc_id, targeted.chunk_id
    ).generation == 1


def test_rrf_uses_channel_ranks(make_container) -> None:
    container, runtime = make_container({})
    doc_id = container.document_service.create(
        tenant_id="tenant_test",
        name="tiny.txt",
        media_type="text/plain",
        data=b"alpha beta gamma delta",
    )
    ingestion = IngestionService(
        container.session_factory,
        container.document_service,
        runtime,
        binding("embedding", "embedding"),
        parent_chars=50,
        child_chars=10,
        overlap_chars=0,
    )
    ingestion.run("tenant_test", doc_id)
    chunks = list(container.document_service.index.chunks.values())
    children = [item for item in chunks if item.chunk_level == "child"]
    fused = rrf(children[:2], list(reversed(children[:2])))
    assert {item.chunk_id for item, _score in fused} == {
        item.chunk_id for item in children[:2]
    }


def test_document_reconciler_marks_missing_and_removes_explicit_orphan(
    make_container, session_factory
) -> None:
    container, _ = make_container({})
    doc_id = container.document_service.create(
        tenant_id="tenant_test",
        name="missing.txt",
        media_type="text/plain",
        data=b"historical",
    )
    with session_factory() as session:
        document = session.scalar(select(DocumentRow).where(DocumentRow.doc_id == doc_id))
        assert document is not None
        container.document_service.object_store.delete(document.storage_ref)
    orphan_temp = container.document_service.object_store.put_temp(
        b"orphan", prefix="tenant_test/doc_orphan"
    )
    orphan_ref = "tenant_test/doc_orphan/original"
    container.document_service.object_store.promote(orphan_temp, orphan_ref)

    result = container.document_service.reconcile(
        "tenant_test", delete_orphans=True
    )

    assert result == {"missing": 1, "orphaned": 1, "removed_orphan": 1}
    assert not container.document_service.object_store.exists(orphan_ref)
    with session_factory() as session:
        document = session.get(DocumentRow, doc_id)
        ingestion = session.get(DocumentIngestionRow, doc_id)
        assert document is not None and document.status == "failed"
        assert ingestion is not None and ingestion.error_code == "object_missing"


def test_document_content_api_and_soft_delete_retention(make_container) -> None:
    container, runtime = make_container({})
    client = TestClient(create_app(container))
    response = client.post(
        "/api/v1/documents",
        headers={"Idempotency-Key": "upload-1"},
        files={"file": ("source.txt", b"retained evidence", "text/plain")},
    )
    assert response.status_code == 201
    doc_id = response.json()["doc_id"]

    second = client.post(
        "/api/v1/documents",
        headers={"Idempotency-Key": "upload-2"},
        files={"file": ("second.md", b"second", "text/markdown")},
    )
    assert second.status_code == 201
    first_page = client.get("/api/v1/documents?limit=1").json()
    assert len(first_page["documents"]) == 1
    assert first_page["next_cursor"]
    second_page = client.get(
        "/api/v1/documents", params={"limit": 1, "cursor": first_page["next_cursor"]}
    ).json()
    assert len(second_page["documents"]) == 1
    assert {
        first_page["documents"][0]["doc_id"],
        second_page["documents"][0]["doc_id"],
    } == {doc_id, second.json()["doc_id"]}

    assert container.document_worker.run(doc_id) == 1
    child = next(
        item
        for item in container.document_service.index.chunks.values()
        if item.doc_id == doc_id and item.chunk_level == "child"
    )

    content = client.get(f"/api/v1/documents/{doc_id}/content?disposition=attachment")
    assert content.status_code == 200
    assert content.content == b"retained evidence"
    assert "attachment" in content.headers["content-disposition"]
    assert "storage_ref" not in response.json()

    assert client.delete(f"/api/v1/documents/{doc_id}").status_code == 204
    assert client.delete(f"/api/v1/documents/{doc_id}").status_code == 204
    assert client.get(f"/api/v1/documents/{doc_id}").status_code == 404
    assert container.document_service.generations("tenant_test") == {}
    assert container.document_service.read_chunk(
        "tenant_test", doc_id, child.chunk_id
    ).text == "retained evidence"


class _ElasticResponse:
    def __init__(self, status_code, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload or {}
        self.text = text

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class _ElasticClient:
    def __init__(self):
        self.mapping = None
        self.bulk_calls = 0

    def get(self, url):
        if self.mapping is None:
            return _ElasticResponse(404)
        return _ElasticResponse(
            200,
            {"document_chunks_v1": {"mappings": self.mapping["mappings"]}},
        )

    def put(self, url, json):
        self.mapping = json
        return _ElasticResponse(200)

    def post(self, url, **kwargs):
        if url.endswith("/_analyze"):
            return _ElasticResponse(200, {"tokens": []})
        self.bulk_calls += 1
        return _ElasticResponse(200, {"errors": False})


def test_elasticsearch_mapping_contains_ik_and_vector_identity() -> None:
    client = _ElasticClient()
    index = ElasticsearchDocumentIndex("http://elastic.test", client=client)
    index.index_generation(
        [
            IndexedChunk(
                tenant_id="tenant_test",
                doc_id="doc",
                generation=1,
                chunk_id="chunk",
                parent_id="parent",
                chunk_level="child",
                text="检索 evidence",
                embedding=[0.1, 0.2, 0.3],
                content_hash="a" * 64,
                chunk_hash="b" * 64,
                tags=[],
            )
        ]
    )
    properties = client.mapping["mappings"]["properties"]
    assert properties["text"]["analyzer"] == "ik_max_word"
    assert properties["embedding"]["dims"] == 3
    assert client.bulk_calls == 1
