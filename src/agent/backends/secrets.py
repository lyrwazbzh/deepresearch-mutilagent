from __future__ import annotations

import base64
import json
import os
import secrets as random_secrets
from dataclasses import dataclass
from datetime import datetime
from typing import Mapping

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from sqlalchemy import func, select

from src.agent.backends.persistence.database import SessionFactory
from src.agent.backends.persistence.models import SecretRow, utcnow


class SecretUnavailableError(PermissionError):
    """Fail-closed error that deliberately does not disclose secret metadata."""


@dataclass(frozen=True)
class SecretMetadata:
    tenant_id: str
    secret_ref: str
    revision: int
    key_id: str
    created_at: datetime
    revoked_at: datetime | None


class SecretKeyring:
    """Deployment-owned AES-256 keys addressed by stable key identifiers."""

    def __init__(self, *, active_key_id: str, keys: Mapping[str, bytes]) -> None:
        normalized = {str(key_id): bytes(key) for key_id, key in keys.items()}
        if not active_key_id or active_key_id not in normalized:
            raise ValueError("The active Secret Store key is unavailable")
        if any(len(key) != 32 for key in normalized.values()):
            raise ValueError("Secret Store keys must be 32-byte AES-256 keys")
        self.active_key_id = active_key_id
        self._keys = normalized

    @classmethod
    def from_environment(cls) -> SecretKeyring | None:
        active_key_id = os.getenv(
            "DEEPRESEARCH_MASTER_KEY_ID", os.getenv("MASTER_KEY_ID", "deployment-master-key")
        )
        keys: dict[str, bytes] = {}
        serialized = os.getenv("DEEPRESEARCH_MASTER_KEYS_JSON") or os.getenv(
            "MASTER_KEYS_JSON"
        )
        if serialized:
            try:
                payload = json.loads(serialized)
            except json.JSONDecodeError as exc:
                raise ValueError("Secret Store keyring JSON is invalid") from exc
            if not isinstance(payload, dict):
                raise ValueError("Secret Store keyring must be a JSON object")
            for key_id, encoded in payload.items():
                if not isinstance(key_id, str) or not isinstance(encoded, str):
                    raise ValueError("Secret Store keyring entries must be strings")
                keys[key_id] = _decode_key(encoded)
        active_encoded = os.getenv("DEEPRESEARCH_MASTER_KEY") or os.getenv("MASTER_KEY")
        if active_encoded:
            keys[active_key_id] = _decode_key(active_encoded)
        if not keys:
            return None
        return cls(active_key_id=active_key_id, keys=keys)

    def active_key(self) -> bytes:
        return self._keys[self.active_key_id]

    def key(self, key_id: str) -> bytes:
        try:
            return self._keys[key_id]
        except KeyError as exc:
            raise SecretUnavailableError("Secret credential is unavailable") from exc


class SecretsService:
    """Tenant-scoped, revisioned Secret Store backed by authenticated encryption."""

    def __init__(self, session_factory: SessionFactory, keyring: SecretKeyring) -> None:
        self.session_factory = session_factory
        self.keyring = keyring

    @classmethod
    def from_environment(cls, session_factory: SessionFactory) -> SecretsService | None:
        keyring = SecretKeyring.from_environment()
        return cls(session_factory, keyring) if keyring is not None else None

    def put(self, tenant_id: str, secret_ref: str, plaintext: bytes | str) -> SecretMetadata:
        tenant_id, secret_ref = _validated_scope(tenant_id, secret_ref)
        payload = plaintext.encode("utf-8") if isinstance(plaintext, str) else bytes(plaintext)
        if not payload:
            raise ValueError("Secret plaintext must not be empty")
        with self.session_factory() as session, session.begin():
            current = session.scalars(
                select(SecretRow)
                .where(
                    SecretRow.tenant_id == tenant_id,
                    SecretRow.secret_ref == secret_ref,
                    SecretRow.revoked_at.is_(None),
                )
                .with_for_update()
            ).all()
            revision = int(
                session.scalar(
                    select(func.max(SecretRow.revision)).where(
                        SecretRow.tenant_id == tenant_id,
                        SecretRow.secret_ref == secret_ref,
                    )
                )
                or 0
            ) + 1
            revoked_at = utcnow()
            for previous in current:
                previous.revoked_at = revoked_at
            session.flush()
            key_id = self.keyring.active_key_id
            nonce = random_secrets.token_bytes(12)
            ciphertext = AESGCM(self.keyring.active_key()).encrypt(
                nonce,
                payload,
                _aad(tenant_id, secret_ref, revision, key_id),
            )
            row = SecretRow(
                tenant_id=tenant_id,
                secret_ref=secret_ref,
                revision=revision,
                key_id=key_id,
                nonce=nonce,
                ciphertext=ciphertext,
            )
            session.add(row)
            session.flush()
            return _metadata(row)

    def resolve(self, tenant_id: str, secret_ref: str) -> bytes:
        tenant_id, secret_ref = _validated_scope(tenant_id, secret_ref)
        with self.session_factory() as session:
            row = session.scalar(
                select(SecretRow)
                .where(
                    SecretRow.tenant_id == tenant_id,
                    SecretRow.secret_ref == secret_ref,
                    SecretRow.revoked_at.is_(None),
                )
                .order_by(SecretRow.revision.desc())
            )
            if row is None:
                raise SecretUnavailableError("Secret credential is unavailable")
            nonce = bytes(row.nonce)
            ciphertext = bytes(row.ciphertext)
            revision = row.revision
            key_id = row.key_id
        try:
            return AESGCM(self.keyring.key(key_id)).decrypt(
                nonce,
                ciphertext,
                _aad(tenant_id, secret_ref, revision, key_id),
            )
        except SecretUnavailableError:
            raise
        except Exception as exc:
            raise SecretUnavailableError("Secret credential is unavailable") from exc

    def resolve_text(self, tenant_id: str, secret_ref: str) -> str:
        try:
            return self.resolve(tenant_id, secret_ref).decode("utf-8")
        except UnicodeDecodeError as exc:
            raise SecretUnavailableError("Secret credential is unavailable") from exc

    def revoke(self, tenant_id: str, secret_ref: str) -> SecretMetadata:
        tenant_id, secret_ref = _validated_scope(tenant_id, secret_ref)
        with self.session_factory() as session, session.begin():
            row = session.scalar(
                select(SecretRow)
                .where(
                    SecretRow.tenant_id == tenant_id,
                    SecretRow.secret_ref == secret_ref,
                    SecretRow.revoked_at.is_(None),
                )
                .order_by(SecretRow.revision.desc())
                .with_for_update()
            )
            if row is None:
                raise SecretUnavailableError("Secret credential is unavailable")
            row.revoked_at = utcnow()
            session.flush()
            return _metadata(row)


def _decode_key(encoded: str) -> bytes:
    try:
        padded = encoded.strip() + "=" * (-len(encoded.strip()) % 4)
        return base64.urlsafe_b64decode(padded.encode("ascii"))
    except (ValueError, UnicodeEncodeError) as exc:
        raise ValueError("Secret Store key must be URL-safe base64") from exc


def _validated_scope(tenant_id: str, secret_ref: str) -> tuple[str, str]:
    tenant_id = tenant_id.strip()
    secret_ref = secret_ref.strip()
    if not tenant_id or len(tenant_id) > 64:
        raise ValueError("tenant_id is invalid")
    if not secret_ref or len(secret_ref) > 255:
        raise ValueError("secret_ref is invalid")
    return tenant_id, secret_ref


def _aad(tenant_id: str, secret_ref: str, revision: int, key_id: str) -> bytes:
    return json.dumps(
        [tenant_id, secret_ref, revision, key_id],
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")


def _metadata(row: SecretRow) -> SecretMetadata:
    return SecretMetadata(
        tenant_id=row.tenant_id,
        secret_ref=row.secret_ref,
        revision=row.revision,
        key_id=row.key_id,
        created_at=row.created_at,
        revoked_at=row.revoked_at,
    )
