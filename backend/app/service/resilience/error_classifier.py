"""Classify failures without retrying validation or authorization errors."""

from __future__ import annotations

import asyncio


def is_retryable(error: BaseException) -> bool:
    if isinstance(error, asyncio.CancelledError):
        return False
    if isinstance(error, (TimeoutError, ConnectionError, OSError)):
        return True
    error_name = type(error).__name__.lower()
    if "timeout" in error_name or error_name in {"connecterror", "connectionerror"}:
        return True
    status_code = getattr(error, "status_code", None)
    if status_code is None:
        response = getattr(error, "response", None)
        status_code = getattr(response, "status_code", None)
    if status_code in {408, 409, 425, 429}:
        return True
    return isinstance(status_code, int) and status_code >= 500
