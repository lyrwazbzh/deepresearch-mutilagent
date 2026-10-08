from __future__ import annotations

import argparse
import sys

from minio import Minio
from redis import Redis
from sqlalchemy import select, text

from src.agent.config import Settings
from src.agent.backends.persistence.database import create_session_factory


SCHEMA_HEAD = "0010_phase14_secret_store"


def _postgres(settings: Settings) -> None:
    sessions = create_session_factory(settings.database_url)
    with sessions() as session:
        session.execute(select(1)).scalar_one()
        if session.get_bind().dialect.name == "postgresql":
            revision = session.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
            if revision != SCHEMA_HEAD:
                raise RuntimeError("schema revision mismatch")


def _redis(settings: Settings) -> None:
    if not Redis.from_url(settings.redis_url, socket_timeout=3).ping():
        raise RuntimeError("Redis did not answer PING")


def _minio(settings: Settings) -> None:
    if not settings.minio_endpoint:
        raise RuntimeError("MINIO_ENDPOINT is required for this process role")
    client = Minio(
        settings.minio_endpoint,
        access_key=settings.minio_access_key,
        secret_key=settings.minio_secret_key,
        secure=settings.minio_secure,
    )
    buckets = {item.name for item in client.list_buckets()}
    if not {"documents", "workspaces"} <= buckets:
        raise RuntimeError("required private MinIO buckets are missing")


def _elasticsearch(settings: Settings) -> None:
    if not settings.elasticsearch_url:
        raise RuntimeError("ELASTICSEARCH_URL is required for ingestion")
    import httpx

    response = httpx.get(f"{settings.elasticsearch_url.rstrip('/')}/_cluster/health", timeout=3)
    response.raise_for_status()


def check(role: str, settings: Settings | None = None) -> None:
    settings = settings or Settings.from_env()
    _postgres(settings)
    _redis(settings)
    if role in {"ingestion", "export"}:
        _minio(settings)
    if role == "ingestion":
        _elasticsearch(settings)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "role", choices=("dispatcher", "research", "ingestion", "export")
    )
    args = parser.parse_args()
    try:
        check(args.role)
    except Exception as exc:
        print(f"not ready: {type(exc).__name__}: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
    print(f"ready: {args.role}")


if __name__ == "__main__":
    main()
