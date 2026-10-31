"""Cascading deadline helper."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone


@dataclass(frozen=True)
class TimeoutBudget:
    deadline_at: datetime | None

    def remaining_seconds(self) -> float | None:
        if self.deadline_at is None:
            return None
        deadline = self.deadline_at
        if deadline.tzinfo is None:
            deadline = deadline.replace(tzinfo=timezone.utc)
        return max((deadline - datetime.now(timezone.utc)).total_seconds(), 0.0)

    def child_timeout(self, maximum: float) -> float:
        remaining = self.remaining_seconds()
        if remaining is None:
            return maximum
        if remaining <= 0:
            raise TimeoutError("operation deadline exhausted")
        return min(maximum, remaining)
