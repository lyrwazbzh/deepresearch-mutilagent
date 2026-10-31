"""Idempotency ports plus an in-memory implementation for tests."""

from __future__ import annotations

import json
from dataclasses import dataclass
from threading import Lock
from typing import Any, Protocol


@dataclass(frozen=True)
class IdempotencyClaim:
    acquired: bool
    completed: bool = False
    result: Any = None


class IdempotencyStore(Protocol):
    def claim(self, key: str, context: Any) -> IdempotencyClaim: ...
    def complete(self, key: str, result: Any) -> None: ...
    def fail(self, key: str, error: BaseException) -> None: ...


class InMemoryIdempotencyStore:
    def __init__(self):
        self._values: dict[str, dict[str, Any]] = {}
        self._lock = Lock()

    def claim(self, key: str, context: Any) -> IdempotencyClaim:
        with self._lock:
            current = self._values.get(key)
            if current and current["status"] == "completed":
                return IdempotencyClaim(True, True, current["result"])
            if current and current["status"] == "running":
                return IdempotencyClaim(False)
            self._values[key] = {"status": "running", "result": None}
            return IdempotencyClaim(True)

    def complete(self, key: str, result: Any) -> None:
        json.dumps(result, default=str)
        with self._lock:
            self._values[key] = {"status": "completed", "result": result}

    def fail(self, key: str, error: BaseException) -> None:
        with self._lock:
            self._values[key] = {"status": "failed", "result": type(error).__name__}
