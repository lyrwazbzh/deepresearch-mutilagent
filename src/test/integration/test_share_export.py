from __future__ import annotations

import base64
from datetime import timedelta

from fastapi.testclient import TestClient
from sqlalchemy import event, select

from src.api_view.web_main import create_app
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
    utcnow,
)
from src.agent.errors import RateLimitedError
from src.agent.reporting.share_export import CreateExportRequest, CreateShareRequest, PublicRateLimiter


PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUB"
    "AScY42YAAAAASUVORK5CYII="
)


def _completed_report(container, session_factory, *, html_title: bool = False) -> str:
    created = container.task_service.create(
        "tenant_test",
        "user_test",
        f"phase14-{utcnow().timestamp()}",
        __import__("src.agent.task_service", fromlist=["CreateTaskRequest"]).CreateTaskRequest(
            query="Phase 14 delivery"
        ),
    )
    task_id = created.projection.task_id
    evidence_data = {"rows": [["grounded"]]}
    evidence = Evidence(
        evidence_id="ev_phase14",
        hypothesis_id="hypothesis_1",
        summary="<b>Grounded source</b>",
        excerpt="grounded",
        source_type="sql",
        stance="support",
        relevance=1,
        data=evidence_data,
        content_hash=content_hash(evidence_data),
    )
    image = container.workspace_service.publish_bytes(
        tenant_id="tenant_test",
        task_id=task_id,
        path="figures/result.png",
        data=PNG,
        mime_type="image/png",
        operation_key=f"phase14-image-{task_id}",
    )
    report = ReportAst.model_validate(
        {
            "report_version": 1,
            "title": "<script>bad()</script>" if html_title else "Phase 14 report",
            "sections": [
                {
                    "section_id": "summary",
                    "heading": "Summary",
                    "level": 1,
                    "blocks": [
                        {
                            "type": "paragraph",
                            "segments": [
                                {
                                    "type": "claim",
                                    "claim_id": "claim_1",
                                    "text": "Grounded result.",
                                    "citation_ids": ["cite_1"],
                                }
                            ],
                        },
                        {
                            "type": "image",
                            "workspace_path": image.workspace_path,
                            "content_hash": image.content_hash,
                            "alt_text": "Result figure",
                            "citation_ids": ["cite_1"],
                        },
                    ],
                }
            ],
            "citations": [
                {
                    "citation_id": "cite_1",
                    "evidence_id": evidence.evidence_id,
                    "locator": "row 1",
                }
            ],
        }
    )
    report_file = container.workspace_service.publish_bytes(
        tenant_id="tenant_test",
        task_id=task_id,
        path="reports/versions/1/report.json",
        data=canonical_json(report.model_dump(mode="json")),
        mime_type="application/json",
        operation_key=f"phase14-report-{task_id}",
    )
    with session_factory() as session, session.begin():
        task = session.get(ResearchTaskRow, task_id)
        assert task is not None
        state = ResearchState.model_validate(task.latest_state)
        state.evidence_pool = [evidence]
        state.report.final = ReportArtifact(
            version=1,
            workspace_path=report_file.workspace_path,
            content_hash=report_file.content_hash,
        )
        task.latest_state = state.model_dump(mode="json")
        task.status = "completed"
    return task_id


def test_share_freezes_public_projection_assets_and_token_hash(make_container, session_factory):
    container, _ = make_container({})
    container.share_manager.url_validator = lambda value: value
    task_id = _completed_report(container, session_factory, html_title=True)

    created = container.share_manager.create(
        "tenant_test", "user_test", CreateShareRequest(task_id=task_id)
    )
    token = str(created["public_url"]).rsplit("/", 1)[-1]
    with session_factory() as session:
        row = session.get(ShareRow, created["share_id"])
        assert row is not None
        assert row.public_token_hash != token
        assert token not in row.public_token_hash
    _row, snapshot = container.share_manager.read_public(token)
    serialized = str(snapshot)
    assert "evidence_id" not in serialized
    assert "workspace_path" not in serialized
    assert "storage_ref" not in serialized
    assert "&lt;script&gt;" in snapshot["title"]
    asset_id = snapshot["assets"][0]["asset_id"]
    _row, payload, media_type, _digest = container.share_manager.read_asset(token, asset_id)
    assert payload == PNG
    assert media_type == "image/png"
    with session_factory() as session, session.begin():
        task = session.get(ResearchTaskRow, task_id)
        assert task is not None
        state = ResearchState.model_validate(task.latest_state)
        assert state.report.final is not None
        state.report.final = state.report.final.model_copy(update={"version": 2})
        task.latest_state = state.model_dump(mode="json")
    _row, frozen = container.share_manager.read_public(token)
    assert frozen["report_version"] == 1
    with session_factory() as session:
        audits = session.scalars(select(AuditLogRow)).all()
        assert any(item.action == "share.create" for item in audits)
        assert any(item.action == "share.read" for item in audits)
        assert any(item.action == "share.asset.read" for item in audits)
        assert all(token not in item.resource_ref for item in audits)
    try:
        container.share_manager.read_asset(token, "asset_not_in_manifest")
        raise AssertionError("out-of-scope asset must not be readable")
    except Exception as exc:
        assert getattr(exc, "status_code", None) == 404


def test_share_revoke_expiry_and_public_security_headers(make_container, session_factory):
    container, _ = make_container({})
    task_id = _completed_report(container, session_factory)
    created = container.share_manager.create(
        "tenant_test",
        "user_test",
        CreateShareRequest(task_id=task_id, expires_at=utcnow() + timedelta(minutes=5)),
    )
    token = str(created["public_url"]).rsplit("/", 1)[-1]
    client = TestClient(create_app(container))
    response = client.get(f"/api/v1/public/shares/{token}")
    assert response.status_code == 200
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["referrer-policy"] == "no-referrer"
    assert "default-src 'none'" in response.headers["content-security-policy"]
    container.share_manager.revoke("tenant_test", "user_test", str(created["share_id"]))
    assert client.get(f"/api/v1/public/shares/{token}").status_code == 404
    limiter = PublicRateLimiter(requests=1, window_seconds=60)
    limiter.acquire("client", now=1)
    try:
        limiter.acquire("client", now=2)
        raise AssertionError("the second public request must be rate limited")
    except RateLimitedError:
        pass
    default_share = container.share_manager.create(
        "tenant_test", "user_test", CreateShareRequest(task_id=task_id)
    )
    with session_factory() as session, session.begin():
        default_row = session.get(ShareRow, default_share["share_id"])
        assert default_row is not None
        assert timedelta(days=29) < default_row.expires_at - default_row.created_at <= timedelta(
            days=30
        )
        default_row.expires_at = utcnow() - timedelta(seconds=1)
    expired_token = str(default_share["public_url"]).rsplit("/", 1)[-1]
    assert client.get(f"/api/v1/public/shares/{expired_token}").status_code == 404
    try:
        container.share_manager.create(
            "tenant_test",
            "user_test",
            CreateShareRequest(task_id=task_id, expires_at=utcnow() + timedelta(days=91)),
        )
        raise AssertionError("Share expiry above the 90-day maximum must fail")
    except Exception as exc:
        assert getattr(exc, "status_code", None) == 422


def test_export_idempotency_worker_and_duplicate_delivery(make_container, session_factory):
    container, _ = make_container({})
    task_id = _completed_report(container, session_factory)
    request = CreateExportRequest(format="docx")
    created, replayed = container.export_manager.create(
        "tenant_test", "user_test", task_id, "export-once", request
    )
    assert not replayed
    same, replayed = container.export_manager.create(
        "tenant_test", "user_test", task_id, "export-once", request
    )
    assert replayed and same["export_id"] == created["export_id"]
    assert container.export_worker.run(str(created["export_id"]))
    assert not container.export_worker.run(str(created["export_id"]))
    result = container.export_manager.get(
        "tenant_test", "user_test", task_id, str(created["export_id"])
    )
    assert result["status"] == "completed"
    assert result["attempt_count"] == 1
    payload, mime_type, _digest, format = container.export_manager.content(
        "tenant_test", "user_test", task_id, str(created["export_id"])
    )
    assert payload.startswith(b"PK")
    assert format == "docx"
    assert "wordprocessingml" in mime_type
    with session_factory() as session:
        message = session.scalar(
            select(OutboxMessageRow).where(
                OutboxMessageRow.kind == "report.export",
                OutboxMessageRow.aggregate_id == created["export_id"],
            )
        )
        assert message is not None
        assert message.payload_json == {"export_id": created["export_id"]}


def test_pdf_export_is_deterministic_and_expiry_is_410(make_container, session_factory):
    container, _ = make_container({})
    task_id = _completed_report(container, session_factory)
    payloads = []
    last_export_id = ""
    for key in ("pdf-one", "pdf-two"):
        created, _ = container.export_manager.create(
            "tenant_test",
            "user_test",
            task_id,
            key,
            CreateExportRequest(format="pdf"),
        )
        last_export_id = str(created["export_id"])
        assert container.export_worker.run(last_export_id)
        payload, _mime, _digest, _format = container.export_manager.content(
            "tenant_test", "user_test", task_id, last_export_id
        )
        assert payload.startswith(b"%PDF")
        payloads.append(payload)
    assert payloads[0] == payloads[1]
    with session_factory() as session, session.begin():
        row = session.get(ExportJobRow, last_export_id)
        assert row is not None
        row.expires_at = utcnow() - timedelta(seconds=1)
    try:
        container.export_manager.content(
            "tenant_test", "user_test", task_id, last_export_id
        )
        raise AssertionError("expired export must not be downloadable")
    except Exception as exc:
        assert getattr(exc, "status_code", None) == 410


def test_export_omitted_version_semantics_fencing_recovery_and_task_delete(
    make_container, session_factory
):
    container, _ = make_container({})
    task_id = _completed_report(container, session_factory)
    created, _ = container.export_manager.create(
        "tenant_test",
        "user_test",
        task_id,
        "semantic-key",
        CreateExportRequest(format="pdf"),
    )
    try:
        container.export_manager.create(
            "tenant_test",
            "user_test",
            task_id,
            "semantic-key",
            CreateExportRequest(format="pdf", report_version=1),
        )
        raise AssertionError("omitted and explicit report versions are different semantics")
    except Exception as exc:
        assert getattr(exc, "status_code", None) == 409

    claim = container.export_worker._claim(str(created["export_id"]))
    assert claim is not None and claim[-1] == 1
    with session_factory() as session, session.begin():
        row = session.get(ExportJobRow, created["export_id"])
        assert row is not None
        row.updated_at = utcnow() - timedelta(hours=1)
    assert container.export_worker.recover_once() == 1
    assert not container.export_worker._complete(
        str(created["export_id"]), 1, f"reports/exports/{created['export_id']}.pdf"
    )
    with session_factory() as session:
        assert session.get(ExportJobRow, created["export_id"]).status == "pending"

    exhausted, _ = container.export_manager.create(
        "tenant_test",
        "user_test",
        task_id,
        "retry-exhausted-key",
        CreateExportRequest(format="pdf"),
    )
    container.export_worker.max_attempts = 1
    exhausted_claim = container.export_worker._claim(str(exhausted["export_id"]))
    assert exhausted_claim is not None
    with session_factory() as session, session.begin():
        exhausted_row = session.get(ExportJobRow, exhausted["export_id"])
        assert exhausted_row is not None
        exhausted_row.updated_at = utcnow() - timedelta(hours=1)
    assert container.export_worker.recover_once() >= 1
    with session_factory() as session:
        exhausted_row = session.get(ExportJobRow, exhausted["export_id"])
        assert exhausted_row is not None
        assert exhausted_row.status == "failed"
        assert exhausted_row.error_code == "export_timeout"

    second, _ = container.export_manager.create(
        "tenant_test",
        "user_test",
        task_id,
        "delete-key",
        CreateExportRequest(format="docx"),
    )
    share = container.share_manager.create(
        "tenant_test", "user_test", CreateShareRequest(task_id=task_id)
    )
    token = str(share["public_url"]).rsplit("/", 1)[-1]
    container.task_service.delete("tenant_test", "user_test", task_id)
    with session_factory() as session:
        assert session.get(ShareRow, share["share_id"]).revoked_at is not None
        deleted_export = session.get(ExportJobRow, second["export_id"])
        assert deleted_export.status == "failed"
        assert deleted_export.error_code == "task_deleted"
        assert session.scalar(
            select(OutboxMessageRow).where(
                OutboxMessageRow.kind == "research.event.publish",
                OutboxMessageRow.aggregate_id.is_not(None),
            )
        ) is not None
    try:
        container.share_manager.read_public(token)
        raise AssertionError("task deletion must revoke the Share")
    except Exception as exc:
        assert getattr(exc, "status_code", None) == 404


def test_workspace_reconciliation_removes_uncontrolled_share_orphans(
    make_container, session_factory
):
    container, _ = make_container({})
    task_id = _completed_report(container, session_factory)
    orphan = container.workspace_service.publish_bytes(
        tenant_id="tenant_test",
        task_id=task_id,
        path="reports/shares/share_missing/snapshot.json",
        data=b"{}",
        mime_type="application/json",
        operation_key="f" * 64,
    )
    with session_factory() as session, session.begin():
        row = session.scalar(
            select(WorkspaceFileRow).where(
                WorkspaceFileRow.task_id == task_id,
                WorkspaceFileRow.workspace_path == orphan.workspace_path,
            )
        )
        assert row is not None
        row.updated_at = utcnow() - timedelta(hours=2)
    result = container.workspace_service.reconcile(
        "tenant_test", task_id, orphan_grace_seconds=1
    )
    assert result["removed_orphan"] == 1


def test_share_pg_commit_failure_leaves_recoverable_orphan(
    make_container, session_factory
) -> None:
    container, _ = make_container({})
    task_id = _completed_report(container, session_factory)

    def fail_share_commit(session) -> None:
        if any(isinstance(item, ShareRow) for item in session.new):
            raise RuntimeError("injected_share_control_commit_failure")

    event.listen(session_factory.class_, "before_commit", fail_share_commit)
    try:
        try:
            container.share_manager.create(
                "tenant_test", "user_test", CreateShareRequest(task_id=task_id)
            )
            raise AssertionError("the injected control-record commit must fail")
        except RuntimeError as exc:
            assert str(exc) == "injected_share_control_commit_failure"
    finally:
        event.remove(session_factory.class_, "before_commit", fail_share_commit)

    with session_factory() as session, session.begin():
        assert session.scalar(select(ShareRow).where(ShareRow.task_id == task_id)) is None
        orphans = session.scalars(
            select(WorkspaceFileRow).where(
                WorkspaceFileRow.task_id == task_id,
                WorkspaceFileRow.workspace_path.startswith("reports/shares/"),
            )
        ).all()
        assert orphans
        for row in orphans:
            row.updated_at = utcnow() - timedelta(hours=2)
            assert row.storage_ref is not None
            assert container.workspace_service.object_store.exists(row.storage_ref)

    result = container.workspace_service.reconcile(
        "tenant_test", task_id, orphan_grace_seconds=1
    )
    assert result["removed_orphan"] == len(orphans)
    with session_factory() as session:
        assert not session.scalars(
            select(WorkspaceFileRow).where(
                WorkspaceFileRow.task_id == task_id,
                WorkspaceFileRow.workspace_path.startswith("reports/shares/"),
            )
        ).all()
