from __future__ import annotations

import argparse
import json
from typing import Any

from sqlalchemy import inspect, select, text
from sqlalchemy.sql.sqltypes import JSON, String, Text

from src.agent.config import Settings
from src.agent.container import build_container
from src.agent.tools.web import HttpPageProvider, JinaReaderProvider, SSRFGuard
from src.agent.identity import sha256_hex
from src.agent.backends.persistence.models import DocumentRow, NodeEffectRow, WorkspaceFileRow
from src.agent.backends.workspace import MinioObjectStore


def _quote(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def _references(container: Any, needle: str) -> list[dict[str, object]]:
    engine = container.session_factory.kw["bind"]
    schema = inspect(engine)
    found: list[dict[str, object]] = []
    with engine.connect() as connection:
        for table_name in schema.get_table_names():
            for column in schema.get_columns(table_name):
                if not isinstance(column["type"], (String, Text, JSON)):
                    continue
                column_name = str(column["name"])
                count = connection.execute(
                    text(
                        f"SELECT count(*) FROM {_quote(table_name)} "
                        f"WHERE CAST({_quote(column_name)} AS text) LIKE :pattern"
                    ),
                    {"pattern": f"%{needle}%"},
                ).scalar_one()
                if count:
                    found.append(
                        {"table": table_name, "column": column_name, "count": int(count)}
                    )
    return found


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Audit and explicitly dispose known Phase 14 object-store inconsistencies."
    )
    parser.add_argument("--workspace-storage-ref", required=True)
    parser.add_argument("--document-storage-ref", required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    settings = Settings.from_env()
    container = build_container(settings)
    workspace_store = container.workspace_service.object_store
    document_store = container.document_service.object_store
    if not isinstance(workspace_store, MinioObjectStore) or not isinstance(
        document_store, MinioObjectStore
    ):
        raise RuntimeError("Phase 14 storage close-out requires real MinIO backends")

    with container.session_factory() as session:
        workspace = session.scalar(
            select(WorkspaceFileRow).where(
                WorkspaceFileRow.storage_ref == args.workspace_storage_ref
            )
        )
        document = session.scalar(
            select(DocumentRow).where(DocumentRow.storage_ref == args.document_storage_ref)
        )
        workspace_snapshot = (
            {
                "workspace_file_id": workspace.workspace_file_id,
                "tenant_id": workspace.tenant_id,
                "task_id": workspace.task_id,
                "workspace_path": workspace.workspace_path,
                "content_hash": workspace.content_hash,
                "size": workspace.size,
                "status_before": workspace.status,
            }
            if workspace is not None
            else None
        )
        document_snapshot = (
            {"doc_id": document.doc_id, "tenant_id": document.tenant_id}
            if document is not None
            else None
        )

    workspace_exists = workspace_store.exists(args.workspace_storage_ref)
    document_exists = document_store.exists(args.document_storage_ref)
    workspace_needle = (
        str(workspace_snapshot["workspace_path"])
        if workspace_snapshot is not None
        else args.workspace_storage_ref
    )
    workspace_refs = [
        item
        for item in _references(container, workspace_needle)
        if not (
            item["table"] == "workspace_files"
            and item["column"] in {"workspace_path", "storage_ref"}
        )
    ]
    document_id = args.document_storage_ref.rstrip("/").split("/")[-2]
    document_refs = [
        item
        for item in _references(container, document_id)
        if not (item["table"] == "documents" and item["column"] in {"doc_id", "storage_ref"})
    ]
    actions: list[str] = []
    rebuild: dict[str, object] | None = None

    if workspace_snapshot is not None:
        with container.session_factory() as session:
            effects = session.scalars(
                select(NodeEffectRow).where(
                    NodeEffectRow.tenant_id == workspace_snapshot["tenant_id"],
                    NodeEffectRow.task_id == workspace_snapshot["task_id"],
                )
            ).all()
        for effect in effects:
            payload = (effect.result_json or {}).get("result", {}).get("payload", {})
            for reference in payload.get("references", []):
                if reference.get("workspace_path") != workspace_snapshot["workspace_path"]:
                    continue
                rebuild = {
                    "effect_id": effect.effect_id,
                    "operation_key": effect.operation_key,
                    "source_uri": reference.get("source_uri"),
                    "expected_content_hash": reference.get("content_hash"),
                    "expected_size": reference.get("size"),
                    "mime_type": reference.get("mime_type") or "text/plain; charset=utf-8",
                }
                break
            if rebuild is not None:
                break

    if args.apply:
        if workspace_snapshot is None:
            raise RuntimeError("The expected Workspace metadata row no longer exists")
        if workspace_exists:
            raise RuntimeError("The Workspace object is present; no missing-object action is valid")
        rebuilt = False
        if rebuild is not None and isinstance(rebuild.get("source_uri"), str):
            safe_url = SSRFGuard().validate(str(rebuild["source_uri"]))
            providers = [
                JinaReaderProvider(
                    api_key=settings.resolve_secret(settings.jina_secret_ref)
                ),
                HttpPageProvider(),
            ]
            for provider in providers:
                try:
                    _title, content, _published_at = provider.read(safe_url)
                except Exception:
                    continue
                normalized = "\n".join(
                    line.rstrip()
                    for line in content.replace("\r\n", "\n").split("\n")
                ).strip()
                body = normalized.encode("utf-8")
                if (
                    sha256_hex(body) != workspace_snapshot["content_hash"]
                    or len(body) != workspace_snapshot["size"]
                ):
                    continue
                outcome = container.workspace_service.reconcile(
                    str(workspace_snapshot["tenant_id"]),
                    str(workspace_snapshot["task_id"]),
                )
                if outcome["missing"] != 1:
                    raise RuntimeError(
                        f"Workspace reconciliation did not mark one missing row: {outcome}"
                    )
                published = container.workspace_service.publish_bytes(
                    tenant_id=str(workspace_snapshot["tenant_id"]),
                    task_id=str(workspace_snapshot["task_id"]),
                    path=str(workspace_snapshot["workspace_path"]),
                    data=body,
                    mime_type=str(rebuild["mime_type"]),
                    operation_key=str(rebuild["operation_key"]),
                )
                if published.content_hash != workspace_snapshot["content_hash"]:
                    raise RuntimeError("Rebuilt Workspace object hash changed during publish")
                rebuild["provider"] = provider.name
                rebuild["verified_content_hash"] = published.content_hash
                actions.append("workspace_rebuilt_from_node_effect_provenance")
                rebuilt = True
                break
        if not rebuilt:
            outcome = container.workspace_service.reconcile(
                str(workspace_snapshot["tenant_id"]),
                str(workspace_snapshot["task_id"]),
            )
            if outcome["missing"] != 1:
                raise RuntimeError(
                    f"Workspace reconciliation did not mark one missing row: {outcome}"
                )
            actions.append("workspace_metadata_marked_failed")

        if document_snapshot is not None:
            raise RuntimeError("The candidate Document object still has an authoritative PG row")
        if not document_exists:
            raise RuntimeError("The candidate Document orphan no longer exists")
        if document_refs:
            raise RuntimeError("The candidate Document object still has database references")
        document_store.delete(args.document_storage_ref)
        if document_store.exists(args.document_storage_ref):
            raise RuntimeError("Document orphan deletion did not converge")
        actions.append("document_orphan_deleted")

    with container.session_factory() as session:
        status_after = session.scalar(
            select(WorkspaceFileRow.status).where(
                WorkspaceFileRow.storage_ref == args.workspace_storage_ref
            )
        )
    print(
        json.dumps(
            {
                "result": "PASS",
                "mode": "apply" if args.apply else "audit",
                "workspace": {
                    "metadata": workspace_snapshot,
                    "object_exists": workspace_exists,
                    "external_references": workspace_refs,
                    "rebuild_provenance": rebuild,
                    "status_after": status_after,
                },
                "document_orphan": {
                    "metadata": document_snapshot,
                    "object_existed": document_exists,
                    "external_references": document_refs,
                    "object_exists_after": document_store.exists(
                        args.document_storage_ref
                    ),
                },
                "actions": actions,
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
