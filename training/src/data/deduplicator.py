"""Exact and locality-bucketed SimHash duplicate detection."""

from __future__ import annotations

import hashlib
import re
from collections import defaultdict


def normalize_text(value: str) -> str:
    return re.sub(r"\s+", " ", value.strip().lower())


def simhash64(value: str) -> int:
    tokens = re.findall(r"[\w\u4e00-\u9fff]+", normalize_text(value))
    weights = [0] * 64
    for token in tokens:
        digest = int.from_bytes(
            hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest(), "big"
        )
        for bit in range(64):
            weights[bit] += 1 if digest & (1 << bit) else -1
    result = 0
    for bit, weight in enumerate(weights):
        if weight >= 0:
            result |= 1 << bit
    return result


class TrajectoryDeduplicator:
    def __init__(self, *, max_hamming_distance: int = 3, bucket_bits: int = 16):
        self.max_hamming_distance = max_hamming_distance
        self.bucket_bits = bucket_bits
        self._exact: set[str] = set()
        self._buckets: dict[tuple[int, int], list[int]] = defaultdict(list)

    def is_duplicate(self, value: str) -> bool:
        normalized = normalize_text(value)
        digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
        if digest in self._exact:
            return True
        fingerprint = simhash64(normalized)
        band_count = 64 // self.bucket_bits
        mask = (1 << self.bucket_bits) - 1
        keys = [
            (band, (fingerprint >> (band * self.bucket_bits)) & mask)
            for band in range(band_count)
        ]
        candidates = {
            existing
            for key in keys
            for existing in self._buckets[key]
        }
        duplicate = any(
            (fingerprint ^ existing).bit_count() <= self.max_hamming_distance
            for existing in candidates
        )
        if duplicate:
            return True
        self._exact.add(digest)
        for key in keys:
            self._buckets[key].append(fingerprint)
        return False
