"""Circuit-breaker stores for production and unit tests."""

from __future__ import annotations

import time
from collections import defaultdict
from threading import Lock
from typing import Any


class CircuitOpenError(RuntimeError):
    pass


class InMemoryCircuitBreaker:
    def __init__(self):
        self._states: dict[str, dict[str, float]] = defaultdict(
            lambda: {"failures": 0.0, "open_until": 0.0}
        )
        self._lock = Lock()

    def allow(self, key: str) -> bool:
        with self._lock:
            state = self._states[key]
            if state["open_until"] <= time.time():
                if state["open_until"]:
                    state["open_until"] = 0.0
                    state["failures"] = 0.0
                return True
            return False

    def success(self, key: str) -> None:
        with self._lock:
            self._states.pop(key, None)

    def failure(self, key: str, *, threshold: int, recovery_seconds: int) -> None:
        with self._lock:
            state = self._states[key]
            state["failures"] += 1
            if state["failures"] >= threshold:
                state["open_until"] = time.time() + recovery_seconds


class RedisCircuitBreaker:
    def __init__(self, client: Any, *, prefix: str = "resilience:circuit"):
        self.client = client
        self.prefix = prefix

    def _key(self, key: str) -> str:
        return f"{self.prefix}:{key}"

    def allow(self, key: str) -> bool:
        raw = self.client.hget(self._key(key), "open_until")
        if not raw:
            return True
        open_until = float(raw)
        if open_until <= time.time():
            self.client.delete(self._key(key))
            return True
        return False

    def success(self, key: str) -> None:
        self.client.delete(self._key(key))

    def failure(self, key: str, *, threshold: int, recovery_seconds: int) -> None:
        redis_key = self._key(key)
        failures = int(self.client.hincrby(redis_key, "failures", 1))
        if failures >= threshold:
            self.client.hset(redis_key, "open_until", time.time() + recovery_seconds)
        self.client.expire(redis_key, max(recovery_seconds * 2, 60))
