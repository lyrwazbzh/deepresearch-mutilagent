from __future__ import annotations

import json
import os
import time
from pathlib import Path

from sqlalchemy import select

from src.agent.config import Settings
from src.agent.container import build_container
from src.agent.backends.persistence.models import ResearchTaskRow
from src.agent.reporting.share_export import CreateExportRequest


def main() -> None:
    settings = Settings.from_env()
    container = build_container(settings)
    with container.session_factory() as session:
        task = session.scalar(
            select(ResearchTaskRow)
            .where(
                ResearchTaskRow.tenant_id == settings.default_tenant_id,
                ResearchTaskRow.deleted_at.is_(None),
                ResearchTaskRow.status == "completed",
            )
            .order_by(ResearchTaskRow.created_at.desc())
        )
    if task is None:
        raise RuntimeError("no completed Phase 14 task is available for recovery seeding")
    export, _replayed = container.export_manager.create(
        settings.default_tenant_id,
        settings.default_user_id,
        task.task_id,
        f"phase14-redis-cold-{time.time_ns()}",
        CreateExportRequest(format="pdf"),
    )
    result = {"task_id": task.task_id, "export_id": export["export_id"]}
    output = Path(os.environ["PHASE14_RECOVERY_SEED_OUTPUT"])
    output.write_text(json.dumps(result), encoding="utf-8")
    output.chmod(0o600)
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
