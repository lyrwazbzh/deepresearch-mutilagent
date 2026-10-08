from __future__ import annotations

import json
from datetime import timedelta

from sqlalchemy import event, select

from src.agent.config import Settings
from src.agent.container import build_container
from src.agent.schema.state import ResearchState
from src.agent.backends.persistence.models import ResearchTaskRow, ShareRow, WorkspaceFileRow, utcnow
from src.agent.reporting.share_export import CreateShareRequest
from src.agent.backends.workspace import MinioObjectStore


def main() -> None:
    settings = Settings.from_env()
    container = build_container(settings)
    if not isinstance(container.workspace_service.object_store, MinioObjectStore):
        raise RuntimeError("commit-failure acceptance requires real MinIO")

    with container.session_factory() as session:
        candidates = session.scalars(
            select(ResearchTaskRow)
            .where(
                ResearchTaskRow.tenant_id == settings.default_tenant_id,
                ResearchTaskRow.user_id == settings.default_user_id,
                ResearchTaskRow.status == "completed",
                ResearchTaskRow.deleted_at.is_(None),
            )
            .order_by(ResearchTaskRow.updated_at.desc())
        ).all()
        task = next(
            (
                row
                for row in candidates
                if ResearchState.model_validate(row.latest_state).report.final is not None
            ),
            None,
        )
        if task is None:
            raise RuntimeError("No completed real report is available for failure injection")
        task_id = task.task_id
        before_paths = set(
            session.scalars(
                select(WorkspaceFileRow.workspace_path).where(
                    WorkspaceFileRow.task_id == task_id,
                    WorkspaceFileRow.workspace_path.startswith("reports/shares/"),
                )
            ).all()
        )
        share_count_before = len(
            session.scalars(select(ShareRow).where(ShareRow.task_id == task_id)).all()
        )

    def fail_share_control_commit(session) -> None:
        if any(isinstance(item, ShareRow) for item in session.new):
            raise RuntimeError("injected_share_control_commit_failure")

    event.listen(container.session_factory.class_, "before_commit", fail_share_control_commit)
    try:
        try:
            container.share_manager.create(
                settings.default_tenant_id,
                settings.default_user_id,
                CreateShareRequest(task_id=task_id),
            )
            raise RuntimeError("injected commit failure did not fire")
        except RuntimeError as exc:
            if str(exc) != "injected_share_control_commit_failure":
                raise
    finally:
        event.remove(
            container.session_factory.class_, "before_commit", fail_share_control_commit
        )

    with container.session_factory() as session, session.begin():
        share_count_after = len(
            session.scalars(select(ShareRow).where(ShareRow.task_id == task_id)).all()
        )
        orphan_rows = [
            row
            for row in session.scalars(
                select(WorkspaceFileRow).where(
                    WorkspaceFileRow.task_id == task_id,
                    WorkspaceFileRow.workspace_path.startswith("reports/shares/"),
                )
            ).all()
            if row.workspace_path not in before_paths
        ]
        if share_count_after != share_count_before or not orphan_rows:
            raise RuntimeError("failure injection did not produce only Workspace orphans")
        storage_refs = [row.storage_ref for row in orphan_rows if row.storage_ref]
        if len(storage_refs) != len(orphan_rows) or not all(
            container.workspace_service.object_store.exists(ref) for ref in storage_refs
        ):
            raise RuntimeError("MinIO publish did not complete before PG failure")
        for row in orphan_rows:
            row.updated_at = utcnow() - timedelta(hours=2)

    outcome = container.workspace_service.reconcile(
        settings.default_tenant_id, task_id, orphan_grace_seconds=1
    )
    if outcome["removed_orphan"] != len(storage_refs):
        raise RuntimeError(f"orphan reconciliation did not converge: {outcome}")
    if any(container.workspace_service.object_store.exists(ref) for ref in storage_refs):
        raise RuntimeError("reconciliation left a MinIO orphan")
    with container.session_factory() as session:
        remaining = session.scalars(
            select(WorkspaceFileRow).where(
                WorkspaceFileRow.task_id == task_id,
                WorkspaceFileRow.workspace_path.in_(
                    [row.workspace_path for row in orphan_rows]
                ),
            )
        ).all()
        if remaining:
            raise RuntimeError("reconciliation left Workspace orphan metadata")

    print(
        json.dumps(
            {
                "result": "PASS",
                "task_id": task_id,
                "failure_injection": "ShareRow transaction before_commit",
                "minio_publish_before_failure": True,
                "share_control_committed": False,
                "orphan_objects_created": len(storage_refs),
                "reconciliation": outcome,
                "orphan_objects_remaining": 0,
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
