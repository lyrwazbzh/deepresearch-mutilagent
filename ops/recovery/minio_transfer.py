from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath

from minio import Minio


BUCKETS = ("documents", "workspaces")


def _client(prefix: str) -> Minio:
    return Minio(
        os.environ[f"{prefix}_MINIO_ENDPOINT"],
        access_key=os.environ[f"{prefix}_MINIO_ACCESS_KEY"],
        secret_key=os.environ[f"{prefix}_MINIO_SECRET_KEY"],
        secure=os.getenv(f"{prefix}_MINIO_SECURE", "0") in {"1", "true", "True"},
    )


def _safe_path(root: Path, bucket: str, key: str) -> Path:
    relative = PurePosixPath(key)
    if relative.is_absolute() or ".." in relative.parts:
        raise RuntimeError(f"unsafe object key: {bucket}/{key}")
    return root / bucket / Path(*relative.parts)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def backup(root: Path) -> None:
    client = _client("SOURCE")
    inventory: list[dict[str, object]] = []
    for bucket in BUCKETS:
        if not client.bucket_exists(bucket):
            continue
        for item in client.list_objects(bucket, recursive=True):
            target = _safe_path(root, bucket, item.object_name)
            target.parent.mkdir(parents=True, exist_ok=True)
            client.fget_object(bucket, item.object_name, str(target))
            inventory.append(
                {
                    "bucket": bucket,
                    "key": item.object_name,
                    "size": target.stat().st_size,
                    "sha256": _sha256(target),
                }
            )
    inventory.sort(key=lambda item: (str(item["bucket"]), str(item["key"])))
    (root / "inventory.json").write_text(
        json.dumps(inventory, sort_keys=True, separators=(",", ":")),
        encoding="utf-8",
    )
    print(json.dumps({"mode": "backup", "objects": len(inventory)}, sort_keys=True))


def restore(root: Path) -> None:
    client = _client("RESTORE")
    inventory = json.loads((root / "inventory.json").read_text(encoding="utf-8"))
    for bucket in BUCKETS:
        if not client.bucket_exists(bucket):
            client.make_bucket(bucket)
    for item in inventory:
        bucket = str(item["bucket"])
        key = str(item["key"])
        source = _safe_path(root, bucket, key)
        if source.stat().st_size != int(item["size"]) or _sha256(source) != item["sha256"]:
            raise RuntimeError(f"backup checksum mismatch: {bucket}/{key}")
        client.fput_object(bucket, key, str(source))
    print(json.dumps({"mode": "restore", "objects": len(inventory)}, sort_keys=True))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=("backup", "restore"))
    parser.add_argument("root", type=Path)
    args = parser.parse_args()
    args.root.mkdir(parents=True, exist_ok=True)
    if args.mode == "backup":
        backup(args.root)
    else:
        restore(args.root)


if __name__ == "__main__":
    main()
