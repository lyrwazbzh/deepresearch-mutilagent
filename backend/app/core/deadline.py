"""Run-scoped deadline propagated to every external dependency call."""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime


_DEADLINE: ContextVar[datetime | None] = ContextVar("research_deadline", default=None)


def current_deadline() -> datetime | None:
    return _DEADLINE.get()


@contextmanager
def deadline_scope(deadline: datetime):
    token = _DEADLINE.set(deadline)
    try:
        yield deadline
    finally:
        try:
            _DEADLINE.reset(token)
        except ValueError as exc:
            # SSE generators may be closed from a different async Context.
            if "in a different Context" not in str(exc):
                raise
