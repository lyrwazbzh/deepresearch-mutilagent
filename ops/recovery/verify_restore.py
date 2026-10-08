from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

from minio import Minio
from sqlalchemy import create_engine, text


def _digest(client: Minio, bucket: str, key: str) -> str:
    response = client.get_object(bucket, key)
    try:
        digest = hashlib.sha256()
        for chunk in response.stream(1024 * 1024):
            digest.update(chunk)
        return digest.hexdigest()
    finally:
        response.close()
        response.release_conn()


def main() -> None:
    database_url = os.environ["RESTORE_DATABASE_URL"]
    client = Minio(
        os.environ["RESTORE_MINIO_ENDPOINT"],
        access_key=os.environ["RESTORE_MINIO_ACCESS_KEY"],
        secret_key=os.environ["RESTORE_MINIO_SECRET_KEY"],
        secure=os.getenv("RESTORE_MINIO_SECURE", "0") in {"1", "true", "True"},
    )
    missing: list[str] = []
    mismatched: list[str] = []
    orphaned: list[str] = []
    checked = 0
    engine = create_engine(database_url)
    with engine.connect() as connection:
        revision = connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
        workspace = connection.execute(
            text(
                "SELECT storage_ref, content_hash FROM workspace_files "
                "WHERE status='ready' AND storage_ref IS NOT NULL"
            )
        ).all()
        documents = connection.execute(
            text(
                "SELECT storage_ref, content_hash FROM documents "
                "WHERE deleted_at IS NULL"
            )
        ).all()
    expected_objects = {
        "workspaces": {str(key) for key, _expected_hash in workspace},
        "documents": {str(key) for key, _expected_hash in documents},
    }
    for bucket, rows in (("workspaces", workspace), ("documents", documents)):
        for key, expected_hash in rows:
            try:
                actual_hash = _digest(client, bucket, key)
            except Exception:
                missing.append(f"{bucket}/{key}")
                continue
            checked += 1
            if actual_hash != expected_hash:
                mismatched.append(f"{bucket}/{key}")
        if client.bucket_exists(bucket):
            for item in client.list_objects(bucket, recursive=True):
                if item.object_name not in expected_objects[bucket]:
                    orphaned.append(f"{bucket}/{item.object_name}")
    secret_dir = Path(os.environ["RESTORE_SECRET_DIR"])
    if not any(path.is_file() for path in secret_dir.rglob("*")):
        raise RuntimeError("restored KMS/Secret recovery material is empty")
    strict_integrity = os.getenv("STRICT_RESTORE_INTEGRITY", "1") in {
        "1",
        "true",
        "True",
    }
    if (missing or mismatched) and strict_integrity:
        raise RuntimeError(
            f"restore integrity failed: missing={missing[:10]}, mismatched={mismatched[:10]}"
        )
    result = {
        "schema": revision,
        "checked_objects": checked,
        "secret_files": sum(1 for path in secret_dir.rglob("*") if path.is_file()),
        "orphan_count": len(orphaned),
        "orphan_sample": orphaned[:20],
        "missing_count": len(missing),
        "missing_sample": missing[:20],
        "mismatched_count": len(mismatched),
        "mismatched_sample": mismatched[:20],
    }
    if orphaned and os.getenv("STRICT_RESTORE_ORPHANS", "0") in {"1", "true", "True"}:
        raise RuntimeError(f"restore contains orphan objects: {orphaned[:20]}")
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
