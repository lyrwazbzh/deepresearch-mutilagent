from __future__ import annotations

import os
import re
import sys


IMAGE_VARIABLES = (
    "BACKEND_CORE_IMAGE",
    "INGESTION_IMAGE",
    "EXPORT_IMAGE",
    "SANDBOX_IMAGE",
    "FRONTEND_IMAGE",
    "NGINX_IMAGE",
    "POSTGRES_IMAGE",
    "REDIS_IMAGE",
    "MINIO_IMAGE",
    "ELASTICSEARCH_IMAGE",
    "NEO4J_IMAGE",
    "PRIVATE_INFERENCE_IMAGE",
)
PINNED = re.compile(r"^[^\s@]+@sha256:[0-9a-f]{64}$")


def main() -> None:
    invalid = [name for name in IMAGE_VARIABLES if not PINNED.fullmatch(os.getenv(name, ""))]
    if invalid:
        print("Unpinned or missing production image refs: " + ", ".join(invalid), file=sys.stderr)
        raise SystemExit(1)
    print(f"validated {len(IMAGE_VARIABLES)} digest-pinned image refs")


if __name__ == "__main__":
    main()
