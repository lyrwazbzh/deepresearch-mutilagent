from __future__ import annotations

import json

from sqlalchemy import select

from src.agent.config import Settings
from src.agent.container import build_container
from src.agent.backends.persistence.models import DocumentRow, WorkspaceFileRow
from src.agent.backends.workspace import MinioObjectStore


def main() -> None:
    container = build_container(Settings.from_env())
    workspace_store = container.workspace_service.object_store
    document_store = container.document_service.object_store
    if not isinstance(workspace_store, MinioObjectStore) or not isinstance(
        document_store, MinioObjectStore
    ):
        raise RuntimeError("storage integrity acceptance requires real MinIO")

    with container.session_factory() as session:
        workspace = session.scalars(
            select(WorkspaceFileRow).where(
                WorkspaceFileRow.status == "ready",
                WorkspaceFileRow.storage_ref.is_not(None),
            )
        ).all()
        documents = session.scalars(
            select(DocumentRow).where(DocumentRow.deleted_at.is_(None))
        ).all()

    expected = {
        "workspaces": {str(row.storage_ref) for row in workspace},
        "documents": {row.storage_ref for row in documents},
    }
    missing: list[str] = []
    mismatched: list[str] = []
    checked = 0
    for bucket, store, rows in (
        ("workspaces", workspace_store, workspace),
        ("documents", document_store, documents),
    ):
        for row in rows:
            ref = str(row.storage_ref)
            if not store.exists(ref):
                missing.append(f"{bucket}/{ref}")
                continue
            size, digest = store.integrity(ref)
            expected_size = row.size if isinstance(row, WorkspaceFileRow) else row.size_bytes
            if size != expected_size or digest != row.content_hash:
                mismatched.append(f"{bucket}/{ref}")
            checked += 1
    orphaned = [
        f"{bucket}/{ref}"
        for bucket, store in (("workspaces", workspace_store), ("documents", document_store))
        for ref in store.list_prefix("")
        if ref not in expected[bucket]
    ]
    result = {
        "result": "PASS" if not (missing or mismatched or orphaned) else "FAIL",
        "checked_objects": checked,
        "missing_count": len(missing),
        "mismatched_count": len(mismatched),
        "orphan_count": len(orphaned),
        "missing_sample": missing[:20],
        "mismatched_sample": mismatched[:20],
        "orphan_sample": orphaned[:20],
    }
    print(json.dumps(result, sort_keys=True))
    if result["result"] != "PASS":
        raise RuntimeError("PostgreSQL/MinIO object integrity did not converge")


if __name__ == "__main__":
    main()
