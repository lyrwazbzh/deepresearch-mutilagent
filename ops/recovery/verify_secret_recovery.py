from __future__ import annotations

import base64
import hashlib
import json
import sys
from pathlib import Path

from cryptography.hazmat.primitives.ciphers.aead import AESGCM


def main() -> None:
    recovery_root = Path(sys.argv[1])
    manifest_path = next(recovery_root.rglob("secret-recovery.json"), None)
    if manifest_path is None:
        raise RuntimeError("secret-recovery.json is missing from restored recovery material")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    key_path = manifest_path.parent / str(manifest["key_file"])
    key = base64.b64decode(key_path.read_text(encoding="ascii").strip())
    plaintext = AESGCM(key).decrypt(
        base64.b64decode(manifest["nonce"]),
        base64.b64decode(manifest["ciphertext"]),
        str(manifest["key_id"]).encode("utf-8"),
    )
    if hashlib.sha256(plaintext).hexdigest() != manifest["plaintext_sha256"]:
        raise RuntimeError("restored secret recovery material failed the decrypt check")
    print(f"secret recovery verified: key_id={manifest['key_id']}")


if __name__ == "__main__":
    main()
