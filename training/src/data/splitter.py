"""Leakage-resistant split assignment grouped by the original seed."""

from __future__ import annotations

import hashlib
from typing import Literal


SplitName = Literal["train", "dev", "test"]


def assign_split(seed_id: str, *, salt: str = "deepresearch-v1") -> SplitName:
    digest = hashlib.sha256(f"{salt}:{seed_id}".encode("utf-8")).digest()
    bucket = int.from_bytes(digest[:4], "big") % 100
    if bucket < 80:
        return "train"
    if bucket < 90:
        return "dev"
    return "test"
