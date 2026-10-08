from __future__ import annotations

import base64
import json
import os
import time
from pathlib import Path
from typing import Any

from sqlalchemy import select

from src.agent.bootstrap import seed_fake_models
from src.agent.config import Settings
from src.agent.container import build_container
from src.agent.identity import canonical_json, content_hash
from src.agent.schema.report import ReportAst
from src.agent.schema.state import Evidence, ReportArtifact, ResearchState
from src.agent.backends.persistence.models import (
    AuditLogRow,
    ExportJobRow,
    OutboxMessageRow,
    ResearchTaskRow,
    ShareRow,
    WorkspaceFileRow,
)
from src.agent.task_service import CreateTaskRequest
from src.agent.reporting.share_export import CreateExportRequest, CreateShareRequest
from src.agent.backends.workspace import MinioObjectStore


PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUB"
    "AScY42YAAAAASUVORK5CYII="
)


def wait_for(predicate: Any, label: str, timeout: float = 60) -> Any:
    deadline = time.monotonic() + timeout
    last: Any = None
    while time.monotonic() < deadline:
        last = predicate()
        if last:
            return last
        time.sleep(0.25)
    raise TimeoutError(f"Timed out waiting for {label}; last={last!r}")


def main() -> None:
    settings = Settings.from_env()
    seed_fake_models(settings)
    container = build_container(settings)
    if not isinstance(container.workspace_service.object_store, MinioObjectStore):
        raise RuntimeError("Phase 14 real acceptance requires the MinIO Workspace backend")
    tenant = settings.default_tenant_id
    user = settings.default_user_id
    created = container.task_service.create(
        tenant,
        user,
        f"phase14-real-{time.time_ns()}",
        CreateTaskRequest(query="Phase 14 real Share and Export acceptance"),
    )
    task_id = created.projection.task_id
    data = {"rows": [["restorable", "grounded"]]}
    evidence = Evidence(
        evidence_id="ev_phase14_real",
        hypothesis_id="h_phase14",
        summary="Phase 14 authoritative source",
        excerpt="restorable and grounded",
        source_type="sql",
        stance="support",
        relevance=1,
        data=data,
        content_hash=content_hash(data),
    )
    image = container.workspace_service.publish_bytes(
        tenant_id=tenant,
        task_id=task_id,
        path="figures/phase14-real.png",
        data=PNG,
        mime_type="image/png",
        operation_key=f"phase14-real-image-{task_id}",
    )
    report = ReportAst.model_validate(
        {
            "report_version": 1,
            "title": "Phase 14 Real Acceptance",
            "sections": [
                {
                    "section_id": "result",
                    "heading": "Result",
                    "level": 1,
                    "blocks": [
                        {
                            "type": "paragraph",
                            "segments": [
                                {
                                    "type": "claim",
                                    "claim_id": "claim_real",
                                    "text": "The delivery chain is grounded.",
                                    "citation_ids": ["citation_real"],
                                }
                            ],
                        },
                        {
                            "type": "image",
                            "workspace_path": image.workspace_path,
                            "content_hash": image.content_hash,
                            "alt_text": "Phase 14 acceptance pixel",
                            "citation_ids": ["citation_real"],
                        },
                    ],
                }
            ],
            "citations": [
                {
                    "citation_id": "citation_real",
                    "evidence_id": evidence.evidence_id,
                    "locator": "row 1",
                }
            ],
        }
    )
    report_file = container.workspace_service.publish_bytes(
        tenant_id=tenant,
        task_id=task_id,
        path="reports/versions/1/report.json",
        data=canonical_json(report.model_dump(mode="json")),
        mime_type="application/json",
        operation_key=f"phase14-real-report-{task_id}",
    )
    with container.session_factory() as session, session.begin():
        task = session.get(ResearchTaskRow, task_id)
        if task is None:
            raise RuntimeError("acceptance task disappeared")
        state = ResearchState.model_validate(task.latest_state)
        state.evidence_pool = [evidence]
        state.report.final = ReportArtifact(
            version=1,
            workspace_path=report_file.workspace_path,
            content_hash=report_file.content_hash,
        )
        task.latest_state = state.model_dump(mode="json")
        task.status = "completed"

    share = container.share_manager.create(
        tenant, user, CreateShareRequest(task_id=task_id)
    )
    token = str(share["public_url"]).rsplit("/", 1)[-1]
    secret_output = os.getenv("PHASE14_ACCEPTANCE_SECRET_OUTPUT")
    if secret_output:
        secret_path = Path(secret_output)
        secret_path.write_text(
            json.dumps({"share_id": share["share_id"], "token": token}),
            encoding="utf-8",
        )
        secret_path.chmod(0o600)
    _share_row, snapshot = container.share_manager.read_public(token)
    asset_id = str(snapshot["assets"][0]["asset_id"])
    _row, asset, _mime, _hash = container.share_manager.read_asset(token, asset_id)
    if asset != PNG or "workspace_path" in json.dumps(snapshot):
        raise RuntimeError("public Share projection or copied Asset is invalid")

    export_ids: dict[str, str] = {}
    for format in ("docx", "pdf"):
        export, _replayed = container.export_manager.create(
            tenant,
            user,
            task_id,
            f"phase14-real-{format}-{time.time_ns()}",
            CreateExportRequest(format=format),
        )
        export_ids[format] = str(export["export_id"])

    def completed() -> bool:
        with container.session_factory() as session:
            rows = session.scalars(
                select(ExportJobRow).where(
                    ExportJobRow.export_id.in_(export_ids.values())
                )
            ).all()
            return len(rows) == 2 and all(row.status == "completed" for row in rows)

    wait_for(completed, "Celery ExportJobs")
    downloads: dict[str, dict[str, object]] = {}
    for format, export_id in export_ids.items():
        payload, mime_type, digest, resolved_format = container.export_manager.content(
            tenant, user, task_id, export_id
        )
        if resolved_format != format:
            raise RuntimeError("export format changed")
        if format == "docx" and not payload.startswith(b"PK"):
            raise RuntimeError("DOCX signature is invalid")
        if format == "pdf" and not payload.startswith(b"%PDF"):
            raise RuntimeError("PDF signature is invalid")
        downloads[format] = {
            "bytes": len(payload),
            "content_hash": digest,
            "media_type": mime_type,
        }

    with container.session_factory() as session:
        share_row = session.get(ShareRow, share["share_id"])
        token_hashed = bool(share_row and share_row.public_token_hash != token)
        outbox = session.scalars(
            select(OutboxMessageRow).where(
                OutboxMessageRow.kind == "report.export",
                OutboxMessageRow.aggregate_id.in_(export_ids.values()),
            )
        ).all()
        workspace_rows = session.scalars(
            select(WorkspaceFileRow).where(
                WorkspaceFileRow.task_id == task_id,
                WorkspaceFileRow.workspace_path.startswith("reports/"),
            )
        ).all()
        audits = session.scalars(
            select(AuditLogRow.action).where(AuditLogRow.tenant_id == tenant)
        ).all()
    if not token_hashed or any(row.payload_json != {"export_id": row.aggregate_id} for row in outbox):
        raise RuntimeError("token hashing or minimal Outbox payload failed")
    required_audits = {
        "share.create",
        "share.read",
        "share.asset.read",
        "export.create",
        "export.download",
    }
    if not required_audits <= set(audits):
        raise RuntimeError("Phase 14 audit actions are incomplete")
    print(
        json.dumps(
            {
                "result": "PASS",
                "task_id": task_id,
                "share_id": share["share_id"],
                "snapshot_path": share_row.snapshot_workspace_path if share_row else None,
                "export_ids": export_ids,
                "downloads": downloads,
                "workspace_files": len(workspace_rows),
                "workspace_backend": type(container.workspace_service.object_store).__name__,
                "audits": sorted(required_audits),
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
