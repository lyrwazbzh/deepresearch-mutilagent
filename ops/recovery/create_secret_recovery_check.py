from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import sys
from pathlib import Path

from cryptography.hazmat.primitives.ciphers.aead import AESGCM


def main() -> None:
    destination = Path(sys.argv[1])
    destination.mkdir(parents=True, exist_ok=True)
    key_id = os.getenv("SECRET_RECOVERY_KEY_ID", "deployment-master-key")
    plaintext = os.environ["SECRET_RECOVERY_CHECK_VALUE"].encode("utf-8")
    key = AESGCM.generate_key(bit_length=256)
    nonce = secrets.token_bytes(12)
    ciphertext = AESGCM(key).encrypt(nonce, plaintext, key_id.encode("utf-8"))
    (destination / "master.key.b64").write_text(
        base64.b64encode(key).decode("ascii"), encoding="ascii"
    )
    os.chmod(destination / "master.key.b64", 0o600)
    (destination / "secret-recovery.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "key_id": key_id,
                "key_file": "master.key.b64",
                "nonce": base64.b64encode(nonce).decode("ascii"),
                "ciphertext": base64.b64encode(ciphertext).decode("ascii"),
                "plaintext_sha256": hashlib.sha256(plaintext).hexdigest(),
            },
            sort_keys=True,
            separators=(",", ":"),
        ),
        encoding="utf-8",
    )
    print(destination)


if __name__ == "__main__":
    main()
