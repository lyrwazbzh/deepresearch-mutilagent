from __future__ import annotations

import json
import os
import time
from pathlib import Path

from sqlalchemy import select

from src.agent.config import Settings
from src.agent.container import build_container
from src.agent.backends.persistence.models import ExportJobRow, ResearchEventRow, ShareRow
from src.agent.backends.workspace import MinioObjectStore


def main() -> None:
    settings = Settings.from_env()
    container = build_container(settings)
    if not isinstance(container.workspace_service.object_store, MinioObjectStore):
        raise RuntimeError("restore acceptance requires the restored MinIO backend")
    share_secret = json.loads(Path(os.environ["PHASE14_SHARE_SECRET_FILE"]).read_text())
    seed = json.loads(Path(os.environ["PHASE14_RECOVERY_SEED_OUTPUT"]).read_text())
    deadline = time.monotonic() + 90
    recovery_status: str | None = None
    while time.monotonic() < deadline:
        with container.session_factory() as session:
            recovery = session.get(ExportJobRow, seed["export_id"])
            recovery_status = recovery.status if recovery is not None else None
        if recovery_status == "completed":
            break
        if recovery_status == "failed":
            raise RuntimeError("cold-recovery ExportJob failed")
        time.sleep(0.5)
    if recovery_status != "completed":
        raise TimeoutError(f"cold-recovery ExportJob did not complete: {recovery_status}")
    share_row, snapshot = container.share_manager.read_public(str(share_secret["token"]))
    asset_id = str(snapshot["assets"][0]["asset_id"])
    _share, asset, media_type, _digest = container.share_manager.read_asset(
        str(share_secret["token"]), asset_id
    )
    restored_exports: list[dict[str, object]] = []
    with container.session_factory() as session:
        exports = session.scalars(
            select(ExportJobRow).where(
                ExportJobRow.task_id == share_row.task_id,
                ExportJobRow.status == "completed",
            )
        ).all()
        event_count = len(
            session.scalars(
                select(ResearchEventRow).where(ResearchEventRow.task_id == share_row.task_id)
            ).all()
        )
        pending = session.get(ExportJobRow, seed["export_id"])
        persisted_share = session.get(ShareRow, share_secret["share_id"])
    for row in exports:
        payload, export_media_type, digest, export_format = container.export_manager.content(
            row.tenant_id,
            settings.default_user_id,
            row.task_id,
            row.export_id,
        )
        restored_exports.append(
            {
                "export_id": row.export_id,
                "format": export_format,
                "bytes": len(payload),
                "content_hash": digest,
                "media_type": export_media_type,
            }
        )
    if (
        persisted_share is None
        or not asset
        or not media_type.startswith("image/")
        or not restored_exports
        or pending is None
        or pending.status != "completed"
    ):
        raise RuntimeError("restored Share, Asset, Export, or pending recovery job is invalid")
    print(
        json.dumps(
            {
                "result": "PASS",
                "share_id": share_row.share_id,
                "share_asset_bytes": len(asset),
                "exports": restored_exports,
                "event_replay_rows": event_count,
                "recovery_export_id": pending.export_id,
                "recovery_export_status": pending.status,
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
