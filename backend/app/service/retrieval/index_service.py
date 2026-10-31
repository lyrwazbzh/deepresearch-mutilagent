"""Idempotent Elasticsearch indexing for knowledge chunks."""

from __future__ import annotations

import logging
import hashlib
from typing import Any, Callable

from .bm25_retriever import KNOWLEDGE_CHUNK_INDEX, normalized_kb_name


logger = logging.getLogger(__name__)


INDEX_MAPPING = {
    "mappings": {
        "properties": {
            "chunk_id": {"type": "keyword"},
            "document_id": {"type": "keyword"},
            "kb_name": {"type": "keyword"},
            "user_id": {"type": "keyword"},
            "filename": {"type": "text", "fields": {"keyword": {"type": "keyword"}}},
            "content": {"type": "text"},
            "chunk_index": {"type": "integer"},
            "metadata": {"type": "object", "enabled": False},
        }
    }
}


class KnowledgeIndexService:
    def __init__(
        self,
        client: Any,
        *,
        index_name: str = KNOWLEDGE_CHUNK_INDEX,
        enqueue_failure: Callable[[str, dict[str, Any]], None] | None = None,
    ):
        self.client = client
        self.index_name = index_name
        self.enqueue_failure = enqueue_failure

    def ensure_index(self) -> None:
        if not self.client.indices.exists(index=self.index_name):
            self.client.indices.create(index=self.index_name, **INDEX_MAPPING)

    def index_chunks(
        self,
        *,
        kb_name: str,
        documents: list[dict[str, Any]],
        user_id: str | None = None,
        enqueue_on_failure: bool = True,
    ) -> bool:
        payload = {
            "kb_name": normalized_kb_name(kb_name),
            "user_id": user_id,
            "documents": [
                {
                    "chunk_id": str(document["id"]),
                    "document_id": str(document.get("doc_id", "")),
                    "filename": str(document.get("filename", "")),
                    "content": str(document.get("content", "")),
                    "chunk_index": int(document.get("chunk_index", 0)),
                }
                for document in documents
            ],
        }
        try:
            self.ensure_index()
            operations: list[dict[str, Any]] = []
            for document in payload["documents"]:
                storage_id = hashlib.sha256(
                    f"{user_id or 'shared'}:{payload['kb_name']}:{document['chunk_id']}".encode("utf-8")
                ).hexdigest()
                operations.extend(
                    [
                        {"index": {"_index": self.index_name, "_id": storage_id}},
                        {
                            **document,
                            "kb_name": payload["kb_name"],
                            "user_id": user_id or "",
                            "metadata": {},
                        },
                    ]
                )
            response = self.client.bulk(operations=operations, refresh=False)
            if response.get("errors"):
                first_error = next(
                    (
                        action.get("index", {}).get("error")
                        for action in response.get("items", [])
                        if action.get("index", {}).get("error")
                    ),
                    "unknown bulk indexing error",
                )
                raise RuntimeError(f"Elasticsearch bulk indexing failed: {first_error}")
            return True
        except Exception as exc:
            logger.warning("BM25 indexing failed: %s", type(exc).__name__)
            if enqueue_on_failure and self.enqueue_failure:
                self.enqueue_failure("upsert", payload)
            return False
