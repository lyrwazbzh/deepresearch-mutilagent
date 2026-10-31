"""Dead-letter port and a small in-memory test implementation."""

from __future__ import annotations

from threading import Lock
from typing import Any, Protocol


class DeadLetterStore(Protocol):
    def enqueue(self, *, context: Any, error: BaseException, attempts: int) -> str: ...


class InMemoryDeadLetterStore:
    def __init__(self):
        self.items: list[dict[str, Any]] = []
        self._lock = Lock()

    def enqueue(self, *, context: Any, error: BaseException, attempts: int) -> str:
        with self._lock:
            item_id = str(len(self.items) + 1)
            self.items.append({
                "id": item_id,
                "context": context,
                "error_type": type(error).__name__,
                "attempts": attempts,
            })
            return item_id
