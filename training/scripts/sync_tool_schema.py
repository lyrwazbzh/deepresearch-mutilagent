"""Copy and verify the canonical DeepResearch tool schema."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path


def canonical_bytes(path: Path) -> bytes:
    payload = json.loads(path.read_text(encoding="utf-8"))
    return json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def schema_digest(path: Path) -> str:
    return hashlib.sha256(canonical_bytes(path)).hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument(
        "--destination",
        type=Path,
        default=Path("config/deepresearch_tools.v1.json"),
    )
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()

    source_digest = schema_digest(args.source)
    if args.check:
        if not args.destination.exists():
            raise SystemExit(f"missing destination schema: {args.destination}")
        destination_digest = schema_digest(args.destination)
        if source_digest != destination_digest:
            raise SystemExit(
                f"schema drift: source={source_digest} destination={destination_digest}"
            )
        print(source_digest)
        return

    args.destination.parent.mkdir(parents=True, exist_ok=True)
    args.destination.write_bytes(args.source.read_bytes())
    print(source_digest)


if __name__ == "__main__":
    main()
