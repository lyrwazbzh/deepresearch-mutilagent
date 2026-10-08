from __future__ import annotations

import json

import pytest
from sqlalchemy import select

from src.agent.tools.database_read import SecretsCredentialResolver, resolve_database_credential
from src.agent.backends.persistence.models import SecretRow
from src.agent.backends.secrets import SecretKeyring, SecretUnavailableError, SecretsService


def service(session_factory, key: bytes = b"k" * 32) -> SecretsService:
    return SecretsService(
        session_factory,
        SecretKeyring(active_key_id="test-key", keys={"test-key": key}),
    )


def test_secret_store_encrypts_at_rest_and_resolves_jit_datasource_bundle(
    session_factory,
) -> None:
    secrets = service(session_factory)
    plaintext = json.dumps(
        {
            "read": {"username": "reader", "password": "read-password"},
            "write": {"username": "writer", "password": "write-password"},
        }
    )

    metadata = secrets.put("tenant_test", "datasource/main", plaintext)

    assert metadata.revision == 1
    with session_factory() as session:
        row = session.scalar(select(SecretRow))
        assert row is not None
        stored = bytes(row.ciphertext)
        assert b"reader" not in stored
        assert b"read-password" not in stored
        assert len(row.nonce) == 12

    resolver = SecretsCredentialResolver(secrets)
    credential = resolve_database_credential(
        resolver,
        tenant_id="tenant_test",
        credential_ref="datasource/main",
        mode="read",
    )
    assert credential.username == "reader"
    assert credential.password == "read-password"
    with pytest.raises(SecretUnavailableError):
        secrets.resolve("other-tenant", "datasource/main")


def test_secret_rotation_revokes_previous_revision_and_revoke_fails_closed(
    session_factory,
) -> None:
    secrets = service(session_factory)
    secrets.put("tenant_test", "provider/main", "first")
    second = secrets.put("tenant_test", "provider/main", "second")

    assert second.revision == 2
    assert secrets.resolve_text("tenant_test", "provider/main") == "second"
    with session_factory() as session:
        rows = session.scalars(
            select(SecretRow).order_by(SecretRow.revision)
        ).all()
        assert rows[0].revoked_at is not None
        assert rows[1].revoked_at is None

    revoked = secrets.revoke("tenant_test", "provider/main")
    assert revoked.revision == 2
    assert revoked.revoked_at is not None
    with pytest.raises(SecretUnavailableError):
        secrets.resolve("tenant_test", "provider/main")


def test_restored_ciphertext_requires_matching_root_recovery_material(
    session_factory,
) -> None:
    service(session_factory).put("tenant_test", "datasource/recovery", "encrypted")
    wrong_key = service(session_factory, b"w" * 32)

    with pytest.raises(SecretUnavailableError, match="unavailable"):
        wrong_key.resolve("tenant_test", "datasource/recovery")
