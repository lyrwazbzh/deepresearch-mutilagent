from __future__ import annotations

import hashlib
import json
from typing import Any


def canonical_json(value: Any) -> bytes:
    """The one canonical representation used by inline result integrity checks."""
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def sha256_hex(value: bytes | str) -> str:
    payload = value.encode("utf-8") if isinstance(value, str) else value
    return hashlib.sha256(payload).hexdigest()


def content_hash(value: Any) -> str:
    return sha256_hex(canonical_json(value))


def operation_key(tenant_id: str, task_id: str, node_run_id: str, effect_name: str) -> str:
    return sha256_hex(f"{tenant_id}|{task_id}|{node_run_id}|{effect_name}")


def result_key(operation: str, stable_item_slot: str) -> str:
    return sha256_hex(f"{operation}|{stable_item_slot}")


def evidence_id(task_id: str, hypothesis_id: str, branch: str, result: str) -> str:
    return f"ev_{sha256_hex(f'{task_id}|{hypothesis_id}|{branch}|{result}')[:40]}"


def analysis_request_id(
    task_id: str,
    hypothesis_id: str,
    objective: str,
    input_evidence_ids: list[str],
    expected_output: str,
) -> str:
    """Stable semantic identity for a post-merge compute requirement."""

    payload = {
        "task_id": task_id,
        "hypothesis_id": hypothesis_id,
        "objective": " ".join(objective.split()),
        "input_evidence_ids": sorted(set(input_evidence_ids)),
        "expected_output": " ".join(expected_output.split()),
    }
    return f"analysis_{sha256_hex(canonical_json(payload))[:40]}"
