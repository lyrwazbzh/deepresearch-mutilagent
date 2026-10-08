from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from typing import Any

from src.agent.errors import InvalidCursorError


def _b64encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode().rstrip("=")


def _b64decode(value: str) -> bytes:
    decoded = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    if _b64encode(decoded) != value:
        raise ValueError("non-canonical base64url")
    return decoded


def _fingerprint(value: object) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


class CursorCodec:
    def __init__(self, key: str, ttl_seconds: int = 1_800) -> None:
        if not key:
            raise ValueError("Cursor signing key must not be empty.")
        self.key = key.encode()
        self.ttl_seconds = ttl_seconds

    def encode(
        self,
        *,
        resource: str,
        scope: object,
        filters: object,
        sort: str,
        last_key: object,
    ) -> str:
        payload = json.dumps(
            {
                "resource": resource,
                "scope_hash": _fingerprint(scope),
                "filter_hash": _fingerprint(filters),
                "sort": sort,
                "last_key": last_key,
                "exp": int(time.time()) + self.ttl_seconds,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        signature = hmac.new(self.key, payload, hashlib.sha256).digest()
        return f"{_b64encode(payload)}.{_b64encode(signature)}"

    def decode(
        self,
        cursor: str,
        *,
        resource: str,
        scope: object,
        filters: object,
        sort: str,
    ) -> Any:
        try:
            encoded_payload, encoded_signature = cursor.split(".", 1)
            payload_bytes = _b64decode(encoded_payload)
            signature = _b64decode(encoded_signature)
            expected = hmac.new(self.key, payload_bytes, hashlib.sha256).digest()
            if not hmac.compare_digest(signature, expected):
                raise ValueError("signature")
            payload = json.loads(payload_bytes)
            if (
                payload["resource"] != resource
                or payload["scope_hash"] != _fingerprint(scope)
                or payload["filter_hash"] != _fingerprint(filters)
                or payload["sort"] != sort
                or int(payload["exp"]) < int(time.time())
            ):
                raise ValueError("scope")
            return payload["last_key"]
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise InvalidCursorError("Cursor is malformed, expired, or outside this scope.") from exc
