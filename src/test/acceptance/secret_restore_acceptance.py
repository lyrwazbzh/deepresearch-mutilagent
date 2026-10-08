from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

import psycopg
from sqlalchemy import delete, select

from src.agent.tools.database_read import SecretsCredentialResolver, resolve_database_credential
from src.agent.backends.persistence.database import create_session_factory
from src.agent.backends.persistence.models import SecretRow
from src.agent.backends.secrets import SecretKeyring, SecretsService


def _service(database_url: str, key_file: Path, key_id: str) -> SecretsService:
    encoded = key_file.read_text().strip()
    import base64

    padded = encoded + "=" * (-len(encoded) % 4)
    key = base64.urlsafe_b64decode(padded.encode("ascii"))
    return SecretsService(
        create_session_factory(database_url),
        SecretKeyring(active_key_id=key_id, keys={key_id: key}),
    )


def _stored_ciphertext_is_clean(
    service: SecretsService,
    tenant_id: str,
    secret_ref: str,
    forbidden: tuple[bytes, ...],
) -> tuple[int, int]:
    with service.session_factory() as session:
        row = session.scalar(
            select(SecretRow).where(
                SecretRow.tenant_id == tenant_id,
                SecretRow.secret_ref == secret_ref,
                SecretRow.revoked_at.is_(None),
            )
        )
        if row is None:
            raise RuntimeError("Secret Store row is unavailable")
        ciphertext = bytes(row.ciphertext)
        if any(value and value in ciphertext for value in forbidden):
            raise RuntimeError("Secret Store ciphertext contains credential plaintext")
        return row.revision, len(ciphertext)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=("seed", "verify", "cleanup"))
    parser.add_argument("--database-url", required=True)
    parser.add_argument("--key-file", required=True, type=Path)
    parser.add_argument("--key-id", default="phase14-closeout-root-v1")
    parser.add_argument("--tenant-id", required=True)
    parser.add_argument("--secret-ref", required=True)
    args = parser.parse_args()

    username = os.environ["PHASE14_TEST_DB_USERNAME"]
    password = os.environ["PHASE14_TEST_DB_PASSWORD"]
    service = _service(args.database_url, args.key_file, args.key_id)

    if args.mode == "cleanup":
        with service.session_factory() as session, session.begin():
            deleted = session.execute(
                delete(SecretRow).where(
                    SecretRow.tenant_id == args.tenant_id,
                    SecretRow.secret_ref == args.secret_ref,
                )
            ).rowcount
        print(json.dumps({"result": "PASS", "deleted_revisions": deleted}))
        return

    if args.mode == "seed":
        metadata = service.put(
            args.tenant_id,
            args.secret_ref,
            json.dumps(
                {
                    "read": {"username": username, "password": password},
                    "write": {"username": username, "password": password},
                },
                separators=(",", ":"),
            ),
        )
        revision, ciphertext_bytes = _stored_ciphertext_is_clean(
            service,
            args.tenant_id,
            args.secret_ref,
            (username.encode(), password.encode()),
        )
        print(
            json.dumps(
                {
                    "result": "PASS",
                    "stage": "seed",
                    "tenant_id": metadata.tenant_id,
                    "secret_ref": metadata.secret_ref,
                    "revision": revision,
                    "key_id": metadata.key_id,
                    "ciphertext_bytes": ciphertext_bytes,
                    "plaintext_at_rest": False,
                },
                sort_keys=True,
            )
        )
        return

    resolver = SecretsCredentialResolver(service)
    credential = resolve_database_credential(
        resolver,
        tenant_id=args.tenant_id,
        credential_ref=args.secret_ref,
        mode="read",
    )
    revision, ciphertext_bytes = _stored_ciphertext_is_clean(
        service,
        args.tenant_id,
        args.secret_ref,
        (credential.username.encode(), credential.password.encode()),
    )
    with psycopg.connect(
        host=os.environ["PHASE14_TARGET_DB_HOST"],
        port=int(os.environ["PHASE14_TARGET_DB_PORT"]),
        dbname=os.environ["PHASE14_TARGET_DB_NAME"],
        user=credential.username,
        password=credential.password,
        connect_timeout=10,
    ) as connection, connection.cursor() as cursor:
        cursor.execute("SELECT 1, current_database()")
        one, database_name = cursor.fetchone()
    if one != 1:
        raise RuntimeError("Restored credential connection check failed")
    print(
        json.dumps(
            {
                "result": "PASS",
                "stage": "restore_verify",
                "tenant_id": args.tenant_id,
                "secret_ref": args.secret_ref,
                "revision": revision,
                "ciphertext_bytes": ciphertext_bytes,
                "root_recovery_material": "restored_copy",
                "decrypt": "PASS",
                "credential_use": "SELECT 1",
                "connected_database": database_name,
                "plaintext_at_rest": False,
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
