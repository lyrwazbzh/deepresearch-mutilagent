from __future__ import annotations

import io
import math
import re
from collections import Counter
from dataclasses import dataclass
from typing import Any, BinaryIO, Protocol

from bs4 import BeautifulSoup
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import delete, func, select

from src.agent.identity import sha256_hex
from src.agent.schema.model_config import FrozenModelBinding, ModelBindingSnapshot
from src.agent.backends.persistence.database import SessionFactory
from src.agent.backends.persistence.models import (
    DocumentChunkRow,
    DocumentIngestionRow,
    DocumentRow,
    DocumentUploadReservationRow,
    OutboxMessageRow,
    ResearchTaskRow,
    utcnow,
)
from src.agent.backends.observability import Telemetry, TraceScope, add_span_event
from src.agent.runtime.tool_runtime import (
    ToolDefinition,
    ToolExecutionContext,
    ToolReference,
    ToolResult,
)
from src.agent.task_service import new_id
from src.agent.errors import QuotaExceededError
from src.agent.backends.workspace import ObjectStore


class VectorRuntime(Protocol):
    def embed(
        self, tenant_id: str, binding: FrozenModelBinding, texts: list[str]
    ) -> list[list[float]]: ...

    def rerank(
        self,
        tenant_id: str,
        binding: FrozenModelBinding,
        query: str,
        candidates: list[str],
    ) -> list[float]: ...


@dataclass(frozen=True)
class IndexedChunk:
    tenant_id: str
    doc_id: str
    generation: int
    chunk_id: str
    parent_id: str | None
    chunk_level: str
    text: str
    embedding: list[float]
    content_hash: str
    chunk_hash: str
    tags: list[str]
    embedding_model_config_id: str = ""
    embedding_model_version: int = 0
    embedding_dimension: int = 0
    tokenizer_revision: str = "unicode-char-v1"
    index_schema: str = "document_chunks_v1"
    section_title: str | None = None
    page_start: int | None = None
    page_end: int | None = None


class DocumentIndex(Protocol):
    def index_generation(self, chunks: list[IndexedChunk]) -> None: ...

    def refresh(self) -> None: ...

    def bm25(
        self,
        query: str,
        *,
        tenant_id: str,
        generations: dict[str, int],
        doc_ids: set[str] | None,
        tags: set[str] | None,
        limit: int,
    ) -> list[tuple[IndexedChunk, float]]: ...

    def knn(
        self,
        vector: list[float],
        *,
        tenant_id: str,
        generations: dict[str, int],
        doc_ids: set[str] | None,
        tags: set[str] | None,
        limit: int,
        embedding_identity: dict[str, Any] | None = None,
    ) -> list[tuple[IndexedChunk, float]]: ...

    def get(self, tenant_id: str, doc_id: str, chunk_id: str) -> IndexedChunk | None: ...


class InMemoryDocumentIndex:
    def __init__(self) -> None:
        self.chunks: dict[str, IndexedChunk] = {}

    def index_generation(self, chunks: list[IndexedChunk]) -> None:
        for chunk in chunks:
            existing = self.chunks.get(chunk.chunk_id)
            if existing and existing != chunk:
                raise ValueError("chunk_id cannot point to different content")
            self.chunks[chunk.chunk_id] = chunk

    def refresh(self) -> None:
        return None

    def _eligible(
        self,
        *,
        tenant_id: str,
        generations: dict[str, int],
        doc_ids: set[str] | None,
        tags: set[str] | None,
    ) -> list[IndexedChunk]:
        return [
            chunk
            for chunk in self.chunks.values()
            if chunk.tenant_id == tenant_id
            and chunk.chunk_level == "child"
            and generations.get(chunk.doc_id) == chunk.generation
            and (doc_ids is None or chunk.doc_id in doc_ids)
            and (not tags or tags.issubset(set(chunk.tags)))
        ]

    def bm25(self, query: str, **kwargs: Any) -> list[tuple[IndexedChunk, float]]:
        limit = int(kwargs.pop("limit"))
        query_terms = _terms(query)
        scored = []
        for chunk in self._eligible(**kwargs):
            counts = Counter(_terms(chunk.text))
            score = sum(counts[term] for term in query_terms)
            if score:
                scored.append((chunk, float(score)))
        return sorted(scored, key=lambda item: (-item[1], item[0].chunk_id))[:limit]

    def knn(self, vector: list[float], **kwargs: Any) -> list[tuple[IndexedChunk, float]]:
        embedding_identity = kwargs.pop("embedding_identity", None)
        limit = int(kwargs.pop("limit"))
        scored = [
            (chunk, _cosine(vector, chunk.embedding))
            for chunk in self._eligible(**kwargs)
            if embedding_identity is None
            or all(
                getattr(chunk, key) == value
                for key, value in embedding_identity.items()
            )
        ]
        return sorted(scored, key=lambda item: (-item[1], item[0].chunk_id))[:limit]

    def get(self, tenant_id: str, doc_id: str, chunk_id: str) -> IndexedChunk | None:
        chunk = self.chunks.get(chunk_id)
        if chunk and chunk.tenant_id == tenant_id and chunk.doc_id == doc_id:
            return chunk
        return None


class ElasticsearchDocumentIndex:
    """HTTP adapter for the reconstructable `document_chunks` index."""

    def __init__(
        self,
        base_url: str,
        *,
        index_name: str = "document_chunks_v1",
        text_analyzer: str = "ik_max_word",
        search_analyzer: str = "ik_smart",
        client: Any = None,
    ) -> None:
        import httpx

        self.base_url = base_url.rstrip("/")
        self.index_name = index_name
        self.text_analyzer = text_analyzer
        self.search_analyzer = search_analyzer
        self.client = client or httpx.Client(timeout=30)
        self._initialized_dimension: int | None = None

    def index_generation(self, chunks: list[IndexedChunk]) -> None:
        if not chunks:
            return
        dimension = len(chunks[0].embedding)
        if dimension < 1 or any(len(chunk.embedding) != dimension for chunk in chunks):
            raise ValueError("all document embeddings must have one non-zero dimension")
        self._ensure_index(dimension)
        lines: list[str] = []
        import json

        for chunk in chunks:
            lines.append(json.dumps({"index": {"_index": self.index_name, "_id": chunk.chunk_id}}))
            lines.append(json.dumps(chunk.__dict__))
        response = self.client.post(
            f"{self.base_url}/_bulk",
            content=("\n".join(lines) + "\n").encode(),
            headers={"Content-Type": "application/x-ndjson"},
        )
        response.raise_for_status()
        if response.json().get("errors"):
            raise RuntimeError("Elasticsearch bulk indexing failed")

    def _ensure_index(self, dimension: int) -> None:
        if self._initialized_dimension == dimension:
            return
        existing = self.client.get(f"{self.base_url}/{self.index_name}/_mapping")
        if existing.status_code == 404:
            text_mapping: dict[str, Any] = {"type": "text"}
            if self._analyzer_available(self.text_analyzer):
                text_mapping.update(
                    {
                        "analyzer": self.text_analyzer,
                        "search_analyzer": self.search_analyzer,
                    }
                )
            response = self.client.put(
                f"{self.base_url}/{self.index_name}",
                json={
                    "mappings": {
                        "dynamic": "strict",
                        "properties": {
                            "tenant_id": {"type": "keyword"},
                            "doc_id": {"type": "keyword"},
                            "generation": {"type": "integer"},
                            "chunk_id": {"type": "keyword"},
                            "parent_id": {"type": "keyword"},
                            "chunk_level": {"type": "keyword"},
                            "text": text_mapping,
                            "embedding": {
                                "type": "dense_vector",
                                "dims": dimension,
                                "index": True,
                                "similarity": "cosine",
                            },
                            "embedding_model_config_id": {"type": "keyword"},
                            "embedding_model_version": {"type": "integer"},
                            "embedding_dimension": {"type": "integer"},
                            "tokenizer_revision": {"type": "keyword"},
                            "index_schema": {"type": "keyword"},
                            "content_hash": {"type": "keyword"},
                            "chunk_hash": {"type": "keyword"},
                            "tags": {"type": "keyword"},
                            "section_title": {"type": "keyword", "index": False},
                            "page_start": {"type": "integer"},
                            "page_end": {"type": "integer"},
                        },
                    }
                },
            )
            if response.status_code not in {200, 201}:
                # A concurrent creator may win the race. Re-read the authoritative mapping.
                if "resource_already_exists_exception" not in response.text:
                    response.raise_for_status()
            existing = self.client.get(f"{self.base_url}/{self.index_name}/_mapping")
        existing.raise_for_status()
        mapping = existing.json()[self.index_name]["mappings"]["properties"]
        actual_dimension = int(mapping["embedding"]["dims"])
        if actual_dimension != dimension:
            raise ValueError(
                f"embedding dimension {dimension} does not match index dimension {actual_dimension}"
            )
        self._initialized_dimension = dimension

    def _analyzer_available(self, analyzer: str) -> bool:
        response = self.client.post(
            f"{self.base_url}/_analyze",
            json={"analyzer": analyzer, "text": "检索 health check"},
        )
        return response.status_code < 400

    def refresh(self) -> None:
        self.client.post(f"{self.base_url}/{self.index_name}/_refresh").raise_for_status()

    def _filter(self, tenant_id: str, generations: dict[str, int], doc_ids: set[str] | None, tags: set[str] | None) -> list[dict[str, Any]]:
        allowed = doc_ids or set(generations)
        should = [
            {
                "bool": {
                    "filter": [
                        {"term": {"doc_id": doc_id}},
                        {"term": {"generation": generations[doc_id]}},
                    ]
                }
            }
            for doc_id in sorted(allowed)
            if doc_id in generations
        ]
        filters: list[dict[str, Any]] = [
            {"term": {"tenant_id": tenant_id}},
            {"term": {"chunk_level": "child"}},
            {"bool": {"should": should, "minimum_should_match": 1}},
        ]
        if tags:
            filters.append({"terms_set": {"tags": {"terms": sorted(tags), "minimum_should_match_script": {"source": "params.num_terms"}}}})
        return filters

    def bm25(self, query: str, **kwargs: Any) -> list[tuple[IndexedChunk, float]]:
        limit = int(kwargs.pop("limit"))
        filters = self._filter(**kwargs)
        response = self.client.post(
            f"{self.base_url}/{self.index_name}/_search",
            json={"size": limit, "query": {"bool": {"must": {"match": {"text": query}}, "filter": filters}}},
        )
        response.raise_for_status()
        return [(_indexed(hit["_source"]), float(hit["_score"] or 0)) for hit in response.json()["hits"]["hits"]]

    def knn(self, vector: list[float], **kwargs: Any) -> list[tuple[IndexedChunk, float]]:
        embedding_identity = kwargs.pop("embedding_identity", None)
        limit = int(kwargs.pop("limit"))
        filters = self._filter(**kwargs)
        if embedding_identity:
            filters.extend(
                {"term": {key: value}} for key, value in embedding_identity.items()
            )
        response = self.client.post(
            f"{self.base_url}/{self.index_name}/_search",
            json={"size": limit, "knn": {"field": "embedding", "query_vector": vector, "k": limit, "num_candidates": max(50, limit * 5), "filter": {"bool": {"filter": filters}}}},
        )
        response.raise_for_status()
        return [(_indexed(hit["_source"]), float(hit["_score"] or 0)) for hit in response.json()["hits"]["hits"]]

    def get(self, tenant_id: str, doc_id: str, chunk_id: str) -> IndexedChunk | None:
        response = self.client.get(f"{self.base_url}/{self.index_name}/_doc/{chunk_id}")
        if response.status_code == 404:
            return None
        response.raise_for_status()
        chunk = _indexed(response.json()["_source"])
        return chunk if chunk.tenant_id == tenant_id and chunk.doc_id == doc_id else None


class DocumentService:
    def __init__(
        self,
        session_factory: SessionFactory,
        object_store: ObjectStore,
        index: DocumentIndex,
        *,
        tenant_bytes_max: int = 50 * 1024**3,
    ) -> None:
        self.session_factory = session_factory
        self.object_store = object_store
        self.index = index
        self.tenant_bytes_max = tenant_bytes_max

    def create(
        self,
        *,
        tenant_id: str,
        name: str,
        media_type: str,
        data: bytes,
        tags: list[str] | None = None,
        doc_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> str:
        return self.create_stream(
            tenant_id=tenant_id,
            name=name,
            media_type=media_type,
            stream=io.BytesIO(data),
            size_bytes=len(data),
            content_hash=sha256_hex(data),
            tags=tags,
            doc_id=doc_id,
            idempotency_key=idempotency_key,
        )

    def create_stream(
        self,
        *,
        tenant_id: str,
        name: str,
        media_type: str,
        stream: BinaryIO,
        size_bytes: int,
        content_hash: str,
        tags: list[str] | None = None,
        doc_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> str:
        doc_id = doc_id or new_id("doc")
        with self.session_factory() as session:
            used = session.scalar(
                select(func.coalesce(func.sum(DocumentRow.size_bytes), 0)).where(
                    DocumentRow.tenant_id == tenant_id
                )
            )
            if int(used or 0) + size_bytes > self.tenant_bytes_max:
                raise QuotaExceededError("Tenant document storage quota exceeded.")
        temp = self.object_store.put_temp_stream(
            stream, size_bytes, prefix=f"{tenant_id}/{doc_id}"
        )
        stored_size, stored_hash = self.object_store.integrity(temp)
        if stored_size != size_bytes or stored_hash != content_hash:
            self.object_store.delete(temp)
            raise ValueError("Document upload integrity check failed")
        final_ref = f"{tenant_id}/{doc_id}/original"
        self.object_store.promote(temp, final_ref)
        with self.session_factory() as session, session.begin():
            document = DocumentRow(
                doc_id=doc_id,
                tenant_id=tenant_id,
                name=name,
                media_type=media_type,
                tags=tags or [],
                storage_ref=final_ref,
                content_hash=content_hash,
                size_bytes=size_bytes,
                status="pending",
            )
            session.add(document)
            # PostgreSQL enforces the ingestion FK immediately.  Materialize the
            # Document aggregate root before adding its generation record; both
            # writes still commit or roll back in this same transaction.
            session.flush()
            session.add(
                DocumentIngestionRow(
                    doc_id=doc_id,
                    tenant_id=tenant_id,
                    generation=1,
                    status="queued",
                    stage="parsing",
                )
            )
            if idempotency_key:
                reservation = session.get(
                    DocumentUploadReservationRow, (tenant_id, idempotency_key)
                )
                if reservation is None or reservation.doc_id != doc_id:
                    raise RuntimeError("Document upload reservation disappeared")
                reservation.content_hash = content_hash
            session.add(
                OutboxMessageRow(
                    message_id=new_id("outbox"),
                    tenant_id=tenant_id,
                    kind="document.ingest",
                    aggregate_id=doc_id,
                    payload_json={"doc_id": doc_id},
                    idempotency_key=f"document:{doc_id}:generation:1",
                    available_at=utcnow(),
                )
            )
        return doc_id

    def get(self, tenant_id: str, doc_id: str) -> DocumentRow:
        with self.session_factory() as session:
            row = session.scalar(
                select(DocumentRow).where(
                    DocumentRow.tenant_id == tenant_id,
                    DocumentRow.doc_id == doc_id,
                    DocumentRow.deleted_at.is_(None),
                )
            )
            if row is None:
                raise FileNotFoundError(doc_id)
            session.expunge(row)
            return row

    def ingestion(self, tenant_id: str, doc_id: str) -> DocumentIngestionRow:
        self.get(tenant_id, doc_id)
        with self.session_factory() as session:
            row = session.get(DocumentIngestionRow, doc_id)
            if row is None or row.tenant_id != tenant_id:
                raise FileNotFoundError(doc_id)
            session.expunge(row)
            return row

    def read_original(self, tenant_id: str, doc_id: str) -> tuple[DocumentRow, bytes]:
        """Read an active document without exposing its physical object-store key."""
        row = self.get(tenant_id, doc_id)
        return row, self.object_store.get(row.storage_ref)

    def delete(self, tenant_id: str, doc_id: str) -> None:
        """Begin the documented retention lifecycle with an idempotent soft delete."""
        with self.session_factory() as session, session.begin():
            row = session.scalar(
                select(DocumentRow)
                .where(DocumentRow.tenant_id == tenant_id, DocumentRow.doc_id == doc_id)
                .with_for_update()
            )
            if row is None:
                raise FileNotFoundError(doc_id)
            if row.deleted_at is None:
                row.deleted_at = utcnow()
                row.updated_at = utcnow()

    def list_ready(self, tenant_id: str, tags: list[str] | None = None) -> list[DocumentRow]:
        with self.session_factory() as session:
            rows = list(
                session.scalars(
                    select(DocumentRow)
                    .where(
                        DocumentRow.tenant_id == tenant_id,
                        DocumentRow.status == "ready",
                        DocumentRow.deleted_at.is_(None),
                    )
                    .order_by(DocumentRow.updated_at.desc(), DocumentRow.doc_id)
                ).all()
            )
            for row in rows:
                session.expunge(row)
        if tags:
            required = set(tags)
            rows = [row for row in rows if required.issubset(set(row.tags or []))]
        return rows

    def generations(
        self, tenant_id: str, doc_ids: list[str] | None = None, tags: list[str] | None = None
    ) -> dict[str, int]:
        rows = self.list_ready(tenant_id, tags)
        allowed = set(doc_ids) if doc_ids else None
        return {
            row.doc_id: int(row.published_generation)
            for row in rows
            if row.published_generation is not None and (allowed is None or row.doc_id in allowed)
        }

    def read_chunk(self, tenant_id: str, doc_id: str, chunk_id: str) -> IndexedChunk:
        # Exact provenance reads intentionally include soft-deleted documents during
        # retention and historical generations. Candidate retrieval still uses
        # generations(), which exposes only the current published generation.
        with self.session_factory() as session:
            document = session.scalar(
                select(DocumentRow).where(
                    DocumentRow.tenant_id == tenant_id,
                    DocumentRow.doc_id == doc_id,
                )
            )
        if document is None:
            raise FileNotFoundError("Document is not available")
        chunk = self.index.get(tenant_id, doc_id, chunk_id)
        if chunk is None:
            raise FileNotFoundError("Document chunk was not found")
        return chunk

    def verify_chunk_hash(
        self, tenant_id: str, doc_id: str, chunk_id: str, content_hash: str
    ) -> bool:
        chunk = self.read_chunk(tenant_id, doc_id, chunk_id)
        return chunk.content_hash == content_hash

    def read_published_chunk(
        self, tenant_id: str, doc_id: str, chunk_id: str
    ) -> IndexedChunk:
        document = self.get(tenant_id, doc_id)
        if document.status != "ready" or document.published_generation is None:
            raise RuntimeError("document_not_ready")
        chunk = self.index.get(tenant_id, doc_id, chunk_id)
        if chunk is None or chunk.generation != document.published_generation:
            raise FileNotFoundError("Document chunk was not found")
        return chunk

    def queue_rebuild(self, tenant_id: str, doc_id: str) -> int:
        with self.session_factory() as session, session.begin():
            doc = session.scalar(
                select(DocumentRow)
                .where(DocumentRow.tenant_id == tenant_id, DocumentRow.doc_id == doc_id)
                .with_for_update()
            )
            ingestion = session.get(DocumentIngestionRow, doc_id)
            if doc is None or ingestion is None:
                raise FileNotFoundError(doc_id)
            ingestion.generation += 1
            ingestion.status = "queued"
            ingestion.stage = "parsing"
            ingestion.error_code = None
            ingestion.updated_at = utcnow()
            if doc.published_generation is None:
                doc.status = "pending"
            return ingestion.generation

    def reconcile(self, tenant_id: str, *, delete_orphans: bool = False) -> dict[str, int]:
        """Converge missing originals and optionally GC unreferenced final objects.

        Orphan deletion is an explicit maintenance action because object storage and
        PostgreSQL cannot share a transaction. Temporary upload objects are excluded.
        """

        missing = 0
        referenced: set[str] = set()
        with self.session_factory() as session, session.begin():
            rows = session.scalars(
                select(DocumentRow).where(DocumentRow.tenant_id == tenant_id)
            ).all()
            referenced = {row.storage_ref for row in rows}
            for row in rows:
                if row.deleted_at is not None or row.status not in {"pending", "ready"}:
                    continue
                if self.object_store.exists(row.storage_ref):
                    continue
                row.status = "failed"
                row.updated_at = utcnow()
                ingestion = session.get(DocumentIngestionRow, row.doc_id)
                if ingestion is not None:
                    ingestion.status = "failed"
                    ingestion.error_code = "object_missing"
                    ingestion.updated_at = utcnow()
                missing += 1

        orphan_refs = [
            ref
            for ref in self.object_store.list_prefix(f"{tenant_id}/")
            if ref.endswith("/original") and ref not in referenced
        ]
        if delete_orphans:
            for ref in orphan_refs:
                self.object_store.delete(ref)
        return {
            "missing": missing,
            "orphaned": len(orphan_refs),
            "removed_orphan": len(orphan_refs) if delete_orphans else 0,
        }


class DocumentParser:
    def parse(self, media_type: str, data: bytes) -> str:
        if media_type in {"text/plain", "text/markdown"}:
            return data.decode("utf-8")
        if media_type == "text/html":
            soup = BeautifulSoup(data, "html.parser")
            for element in soup(["script", "style", "noscript"]):
                element.decompose()
            return soup.get_text("\n")
        if media_type == "application/pdf":
            try:
                from pypdf import PdfReader
            except ImportError as exc:
                raise RuntimeError("pypdf is required for PDF ingestion") from exc
            return "\n".join(page.extract_text() or "" for page in PdfReader(io.BytesIO(data)).pages)
        if media_type == "application/vnd.openxmlformats-officedocument.wordprocessingml.document":
            try:
                from docx import Document
            except ImportError as exc:
                raise RuntimeError("python-docx is required for DOCX ingestion") from exc
            document = Document(io.BytesIO(data))
            return "\n".join(paragraph.text for paragraph in document.paragraphs)
        raise ValueError("unsupported_media")


class IngestionService:
    def __init__(
        self,
        session_factory: SessionFactory,
        document_service: DocumentService,
        model_runtime: VectorRuntime,
        embedding_binding: FrozenModelBinding,
        *,
        parser: DocumentParser | None = None,
        parent_chars: int = 4800,
        child_chars: int = 1600,
        overlap_chars: int = 320,
    ) -> None:
        self.session_factory = session_factory
        self.documents = document_service
        self.model_runtime = model_runtime
        self.embedding_binding = embedding_binding
        self.parser = parser or DocumentParser()
        self.parent_chars = parent_chars
        self.child_chars = child_chars
        self.overlap_chars = overlap_chars

    def run(self, tenant_id: str, doc_id: str, *, fail_stage: str | None = None) -> int:
        with self.session_factory() as session, session.begin():
            doc = session.scalar(
                select(DocumentRow).where(
                    DocumentRow.tenant_id == tenant_id, DocumentRow.doc_id == doc_id
                )
            )
            ingestion = session.get(DocumentIngestionRow, doc_id)
            if doc is None or ingestion is None or ingestion.status not in {"queued", "failed"}:
                raise RuntimeError("Document ingestion cannot be claimed")
            ingestion.status = "processing"
            ingestion.attempt_count += 1
            ingestion.stage = "parsing"
            ingestion.error_code = None
            generation = ingestion.generation
            attempt = ingestion.attempt_count
            storage_ref = doc.storage_ref
            media_type = doc.media_type
            content_hash = doc.content_hash
            tags = list(doc.tags or [])
        try:
            data = self.documents.object_store.get(storage_ref)
            text = self.parser.parse(media_type, data)
            if not text.strip():
                raise ValueError("parsed document is empty; OCR may be required")
            if fail_stage == "parsing":
                raise RuntimeError("injected parse failure")
            self._stage(doc_id, generation, attempt, "chunking")
            parents, children = _chunk_document(
                tenant_id,
                doc_id,
                generation,
                text,
                content_hash,
                tags,
                parent_chars=self.parent_chars,
                child_chars=self.child_chars,
                overlap_chars=self.overlap_chars,
            )
            self._stage(doc_id, generation, attempt, "embedding")
            embeddings = self.model_runtime.embed(
                tenant_id,
                self.embedding_binding,
                [chunk.text for chunk in [*parents, *children]],
            )
            indexed = [
                IndexedChunk(
                    **{
                        **chunk.__dict__,
                        "embedding": embedding,
                        "embedding_model_config_id": (
                            self.embedding_binding.primary.model_config_id
                        ),
                        "embedding_model_version": self.embedding_binding.primary.version,
                        "embedding_dimension": len(embedding),
                        "tokenizer_revision": "unicode-char-v1",
                        "index_schema": getattr(
                            self.documents.index, "index_name", "document_chunks_v1"
                        ),
                    }
                )
                for chunk, embedding in zip([*parents, *children], embeddings, strict=True)
            ]
            self._stage(doc_id, generation, attempt, "indexing")
            if fail_stage == "indexing":
                raise RuntimeError("injected index failure")
            self.documents.index.index_generation(indexed)
            self.documents.index.refresh()
            with self.session_factory() as session, session.begin():
                ingestion = session.scalar(
                    select(DocumentIngestionRow)
                    .where(
                        DocumentIngestionRow.doc_id == doc_id,
                        DocumentIngestionRow.generation == generation,
                        DocumentIngestionRow.attempt_count == attempt,
                        DocumentIngestionRow.status == "processing",
                    )
                    .with_for_update()
                )
                doc = session.scalar(
                    select(DocumentRow)
                    .where(DocumentRow.tenant_id == tenant_id, DocumentRow.doc_id == doc_id)
                    .with_for_update()
                )
                if ingestion is None or doc is None:
                    raise RuntimeError("stale ingestion worker")
                session.execute(
                    delete(DocumentChunkRow).where(
                        DocumentChunkRow.doc_id == doc_id,
                        DocumentChunkRow.generation == generation,
                    )
                )
                for chunk in indexed:
                    session.add(
                        DocumentChunkRow(
                            chunk_id=chunk.chunk_id,
                            tenant_id=tenant_id,
                            doc_id=doc_id,
                            generation=generation,
                            parent_id=chunk.parent_id,
                            ordinal=int(chunk.chunk_id.rsplit("_", 1)[-1]),
                            section_title=chunk.section_title,
                            page_start=chunk.page_start,
                            page_end=chunk.page_end,
                            chunk_hash=chunk.chunk_hash,
                            metadata_json={
                                "chunk_level": chunk.chunk_level,
                                "embedding_model_config_id": (
                                    chunk.embedding_model_config_id
                                ),
                                "embedding_model_version": chunk.embedding_model_version,
                                "embedding_dimension": chunk.embedding_dimension,
                                "tokenizer_revision": chunk.tokenizer_revision,
                                "index_schema": chunk.index_schema,
                            },
                        )
                    )
                ingestion.stage = "publishing"
                ingestion.status = "succeeded"
                ingestion.updated_at = utcnow()
                doc.published_generation = generation
                doc.status = "ready"
                doc.indexed_at = utcnow()
                doc.updated_at = utcnow()
            return generation
        except Exception:
            with self.session_factory() as session, session.begin():
                ingestion = session.get(DocumentIngestionRow, doc_id)
                doc = session.get(DocumentRow, doc_id)
                if ingestion and ingestion.generation == generation and ingestion.attempt_count == attempt:
                    ingestion.status = "failed"
                    ingestion.error_code = {
                        "embedding": "embedding_failed",
                        "indexing": "index_failed",
                    }.get(ingestion.stage, "parse_failed")
                    ingestion.updated_at = utcnow()
                if doc and doc.published_generation is None:
                    doc.status = "failed"
                    doc.updated_at = utcnow()
            raise

    def _stage(self, doc_id: str, generation: int, attempt: int, stage: str) -> None:
        with self.session_factory() as session, session.begin():
            row = session.scalar(
                select(DocumentIngestionRow)
                .where(
                    DocumentIngestionRow.doc_id == doc_id,
                    DocumentIngestionRow.generation == generation,
                    DocumentIngestionRow.attempt_count == attempt,
                    DocumentIngestionRow.status == "processing",
                )
                .with_for_update()
            )
            if row is None:
                raise RuntimeError("stale ingestion worker")
            row.stage = stage
            row.updated_at = utcnow()


@dataclass(frozen=True)
class RetrievalHit:
    chunk: IndexedChunk
    context_text: str
    score: float


class RetrievalGateway:
    def __init__(
        self,
        documents: DocumentService,
        index: DocumentIndex,
        model_runtime: VectorRuntime,
        telemetry: Telemetry | None = None,
    ) -> None:
        self.documents = documents
        self.index = index
        self.model_runtime = model_runtime
        self.telemetry = telemetry

    def search(
        self,
        *,
        tenant_id: str,
        query: str,
        embedding_binding: FrozenModelBinding,
        rerank_binding: FrozenModelBinding,
        doc_ids: list[str] | None = None,
        tags: list[str] | None = None,
        top_k: int = 10,
    ) -> tuple[list[RetrievalHit], list[str]]:
        query_hash = sha256_hex(query)
        if self.telemetry is None:
            return self._search(
                tenant_id=tenant_id,
                query=query,
                embedding_binding=embedding_binding,
                rerank_binding=rerank_binding,
                doc_ids=doc_ids,
                tags=tags,
                top_k=top_k,
            )
        with self.telemetry.span(
            "retrieval.search",
            scope=TraceScope(tenant_id=tenant_id),
            attributes={"retrieval.query_hash": query_hash},
        ) as span:
            hits, degraded = self._search(
                tenant_id=tenant_id,
                query=query,
                embedding_binding=embedding_binding,
                rerank_binding=rerank_binding,
                doc_ids=doc_ids,
                tags=tags,
                top_k=top_k,
            )
            if span is not None:
                span.set_attribute("retrieval.result_count", len(hits))
            for reason_code in degraded:
                add_span_event("retrieval.degraded", {"reason_code": reason_code})
            return hits, degraded

    def _search(
        self,
        *,
        tenant_id: str,
        query: str,
        embedding_binding: FrozenModelBinding,
        rerank_binding: FrozenModelBinding,
        doc_ids: list[str] | None = None,
        tags: list[str] | None = None,
        top_k: int = 10,
    ) -> tuple[list[RetrievalHit], list[str]]:
        generations = self.documents.generations(tenant_id, doc_ids, tags)
        if not generations:
            return [], []
        vector = self.model_runtime.embed(tenant_id, embedding_binding, [query])[0]
        common: dict[str, Any] = {
            "tenant_id": tenant_id,
            "generations": generations,
            "doc_ids": set(doc_ids) if doc_ids else None,
            "tags": set(tags) if tags else None,
            "limit": min(50, max(top_k * 3, top_k)),
        }
        degraded: list[str] = []
        try:
            lexical = self.index.bm25(query, **common)
        except Exception:
            lexical = []
            degraded.append("bm25")
        try:
            semantic = self.index.knn(
                vector,
                **common,
                embedding_identity={
                    "embedding_model_config_id": (
                        embedding_binding.primary.model_config_id
                    ),
                    "embedding_model_version": embedding_binding.primary.version,
                    "embedding_dimension": len(vector),
                    "tokenizer_revision": "unicode-char-v1",
                    "index_schema": getattr(
                        self.index, "index_name", "document_chunks_v1"
                    ),
                },
            )
        except Exception:
            semantic = []
            degraded.append("knn")
        fused = rrf([chunk for chunk, _ in lexical], [chunk for chunk, _ in semantic])
        candidates = [chunk for chunk, _ in fused[: max(top_k * 3, top_k)]]
        try:
            rerank_scores = self.model_runtime.rerank(
                tenant_id, rerank_binding, query, [chunk.text for chunk in candidates]
            )
            ordered = sorted(
                zip(candidates, rerank_scores, strict=True),
                key=lambda item: (-item[1], item[0].chunk_id),
            )
        except Exception:
            degraded.append("rerank")
            ordered = [(chunk, score) for chunk, score in fused]
        output: list[RetrievalHit] = []
        for chunk, score in ordered[:top_k]:
            parent = self.index.get(tenant_id, chunk.doc_id, chunk.parent_id) if chunk.parent_id else None
            output.append(
                RetrievalHit(
                    chunk=chunk,
                    context_text=parent.text if parent else chunk.text,
                    score=float(score),
                )
            )
        return output, degraded

    def search_bm25_baseline(
        self,
        *,
        tenant_id: str,
        query: str,
        doc_ids: list[str] | None = None,
        tags: list[str] | None = None,
        top_k: int = 5,
    ) -> list[RetrievalHit]:
        """Frozen Evaluation baseline using the same index and parent expansion."""
        generations = self.documents.generations(tenant_id, doc_ids, tags)
        if not generations:
            return []
        ranked = self.index.bm25(
            query,
            tenant_id=tenant_id,
            generations=generations,
            doc_ids=set(doc_ids) if doc_ids else None,
            tags=set(tags) if tags else None,
            limit=top_k,
        )
        output: list[RetrievalHit] = []
        for chunk, score in ranked[:top_k]:
            parent = (
                self.index.get(tenant_id, chunk.doc_id, chunk.parent_id)
                if chunk.parent_id
                else None
            )
            output.append(
                RetrievalHit(
                    chunk=chunk,
                    context_text=parent.text if parent else chunk.text,
                    score=float(score),
                )
            )
        return output


class SearchDocumentsArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    query: str | None = None
    tags: list[str] | None = None
    limit: int = Field(default=20, ge=1, le=100)
    cursor: int | None = Field(default=None, ge=0)


class SearchChunksArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    query: str = Field(min_length=1)
    doc_ids: list[str] | None = None
    tags: list[str] | None = None
    top_k: int = Field(default=10, ge=1, le=20)


class ReadDocumentArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    doc_id: str
    chunk_id: str


class TaskBindingResolver:
    def __init__(self, session_factory: SessionFactory) -> None:
        self.session_factory = session_factory

    def get(self, tenant_id: str, task_id: str, key: str) -> FrozenModelBinding:
        with self.session_factory() as session:
            row = session.scalar(
                select(ResearchTaskRow).where(
                    ResearchTaskRow.tenant_id == tenant_id,
                    ResearchTaskRow.task_id == task_id,
                )
            )
            if row is None:
                raise FileNotFoundError(task_id)
            snapshot = ModelBindingSnapshot.model_validate(row.model_binding_snapshot)
            return snapshot.get(key)  # type: ignore[arg-type]


class SearchDocumentsAdapter:
    def __init__(self, documents: DocumentService) -> None:
        self.documents = documents

    def execute(self, context: ToolExecutionContext, arguments: dict[str, Any]) -> ToolResult:
        query = (arguments.get("query") or "").lower()
        rows = self.documents.list_ready(context.tenant_id, arguments.get("tags"))
        if query:
            rows = [row for row in rows if query in row.name.lower() or any(query in tag.lower() for tag in row.tags or [])]
        start = int(arguments.get("cursor") or 0)
        limit = arguments.get("limit", 20)
        selected = rows[start : start + limit]
        return ToolResult(
            status="success",
            summary=f"{len(selected)} documents",
            data={
                "documents": [
                    {
                        "doc_id": row.doc_id,
                        "name": row.name,
                        "media_type": row.media_type,
                        "tags": row.tags or [],
                        "updated_at": row.updated_at.isoformat(),
                    }
                    for row in selected
                ],
                "truncated": start + limit < len(rows),
            },
            next_cursor=str(start + limit) if start + limit < len(rows) else None,
        )


class SearchChunksAdapter:
    def __init__(self, gateway: RetrievalGateway, bindings: TaskBindingResolver) -> None:
        self.gateway = gateway
        self.bindings = bindings

    def execute(self, context: ToolExecutionContext, arguments: dict[str, Any]) -> ToolResult:
        hits, degraded = self.gateway.search(
            tenant_id=context.tenant_id,
            query=arguments["query"],
            embedding_binding=self.bindings.get(context.tenant_id, context.task_id, "embedding"),
            rerank_binding=self.bindings.get(context.tenant_id, context.task_id, "rerank"),
            doc_ids=arguments.get("doc_ids"),
            tags=arguments.get("tags"),
            top_k=arguments.get("top_k", 10),
        )
        return ToolResult(
            status="success",
            summary=f"{len(hits)} hybrid retrieval candidates",
            data={
                "chunks": [
                    {
                        "doc_id": hit.chunk.doc_id,
                        "chunk_id": hit.chunk.chunk_id,
                        "content_hash": hit.chunk.content_hash,
                        "text": hit.context_text,
                        "score": hit.score,
                        "section_title": hit.chunk.section_title,
                        "page_start": hit.chunk.page_start,
                        "page_end": hit.chunk.page_end,
                    }
                    for hit in hits
                ],
                "degraded_channels": degraded,
                "truncated": False,
            },
        )


class ReadDocumentAdapter:
    def __init__(self, documents: DocumentService) -> None:
        self.documents = documents

    def execute(self, context: ToolExecutionContext, arguments: dict[str, Any]) -> ToolResult:
        chunk = self.documents.read_published_chunk(
            context.tenant_id, arguments["doc_id"], arguments["chunk_id"]
        )
        return ToolResult(
            status="success",
            summary=chunk.text[:500],
            data={
                "doc_id": chunk.doc_id,
                "chunk_id": chunk.chunk_id,
                "content_hash": chunk.content_hash,
                "content": chunk.text,
                "section_title": chunk.section_title,
                "page_start": chunk.page_start,
                "page_end": chunk.page_end,
            },
            references=[
                ToolReference(
                    doc_id=chunk.doc_id,
                    chunk_id=chunk.chunk_id,
                    content_hash=chunk.content_hash,
                )
            ],
        )


def tool_definitions(
    documents: DocumentService,
    gateway: RetrievalGateway,
    bindings: TaskBindingResolver,
) -> list[ToolDefinition]:
    return [
        ToolDefinition(
            name="search_documents",
            args_schema=SearchDocumentsArgs,
            adapter=SearchDocumentsAdapter(documents),
        ),
        ToolDefinition(
            name="search_chunks",
            args_schema=SearchChunksArgs,
            adapter=SearchChunksAdapter(gateway, bindings),
        ),
        ToolDefinition(
            name="read_document",
            args_schema=ReadDocumentArgs,
            adapter=ReadDocumentAdapter(documents),
            evidence_eligible=True,
            effect_target=lambda args: f"{args['doc_id']}:{args['chunk_id']}",
        ),
    ]


def rrf(
    lexical: list[IndexedChunk], semantic: list[IndexedChunk], rank_constant: int = 60
) -> list[tuple[IndexedChunk, float]]:
    scores: dict[str, float] = {}
    chunks: dict[str, IndexedChunk] = {}
    for ranking in (lexical, semantic):
        for rank, chunk in enumerate(ranking, start=1):
            chunks[chunk.chunk_id] = chunk
            scores[chunk.chunk_id] = scores.get(chunk.chunk_id, 0.0) + 1.0 / (
                rank_constant + rank
            )
    return sorted(
        ((chunks[key], score) for key, score in scores.items()),
        key=lambda item: (-item[1], item[0].chunk_id),
    )


def _chunk_document(
    tenant_id: str,
    doc_id: str,
    generation: int,
    text: str,
    content_hash: str,
    tags: list[str],
    *,
    parent_chars: int,
    child_chars: int,
    overlap_chars: int,
) -> tuple[list[IndexedChunk], list[IndexedChunk]]:
    normalized = "\n".join(line.strip() for line in text.splitlines() if line.strip())
    parents: list[IndexedChunk] = []
    children: list[IndexedChunk] = []
    parent_ordinal = 0
    child_ordinal = 0
    for parent_start in range(0, len(normalized), parent_chars):
        parent_text = normalized[parent_start : parent_start + parent_chars]
        parent_id = f"chk_{sha256_hex(f'{doc_id}|{generation}|parent|{parent_ordinal}|{sha256_hex(parent_text)}')[:32]}_{parent_ordinal}"
        parents.append(
            IndexedChunk(
                tenant_id=tenant_id,
                doc_id=doc_id,
                generation=generation,
                chunk_id=parent_id,
                parent_id=None,
                chunk_level="parent",
                text=parent_text,
                embedding=[],
                content_hash=content_hash,
                chunk_hash=sha256_hex(parent_text),
                tags=tags,
            )
        )
        step = max(1, child_chars - overlap_chars)
        for child_start in range(0, len(parent_text), step):
            child_text = parent_text[child_start : child_start + child_chars]
            if not child_text:
                continue
            child_id = f"chk_{sha256_hex(f'{doc_id}|{generation}|child|{child_ordinal}|{sha256_hex(child_text)}')[:32]}_{child_ordinal}"
            children.append(
                IndexedChunk(
                    tenant_id=tenant_id,
                    doc_id=doc_id,
                    generation=generation,
                    chunk_id=child_id,
                    parent_id=parent_id,
                    chunk_level="child",
                    text=child_text,
                    embedding=[],
                    content_hash=content_hash,
                    chunk_hash=sha256_hex(child_text),
                    tags=tags,
                )
            )
            child_ordinal += 1
            if child_start + child_chars >= len(parent_text):
                break
        parent_ordinal += 1
    return parents, children


def _terms(text: str) -> list[str]:
    return re.findall(r"[\w\u4e00-\u9fff]+", text.lower())


def _cosine(left: list[float], right: list[float]) -> float:
    if not left or not right or len(left) != len(right):
        return 0.0
    dot = sum(a * b for a, b in zip(left, right, strict=True))
    norm = math.sqrt(sum(a * a for a in left)) * math.sqrt(sum(b * b for b in right))
    return dot / norm if norm else 0.0


def _indexed(payload: dict[str, Any]) -> IndexedChunk:
    return IndexedChunk(**payload)
