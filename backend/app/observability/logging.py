"""Structured application logging with correlation and data minimization."""

from __future__ import annotations

import json
import logging
import os
import sys
from datetime import UTC, datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path
from types import TracebackType
from typing import Any, Mapping

from .context import current_context


REDACTED = "[REDACTED]"
_SENSITIVE_KEYS = {
    "api-key",
    "api_key",
    "authorization",
    "cookie",
    "password",
    "secret",
    "set-cookie",
    "token",
}


def _is_sensitive_key(key: object) -> bool:
    normalized = str(key).strip().lower()
    return normalized in _SENSITIVE_KEYS or normalized.endswith(("_password", "_secret", "_api_key"))


def redact(value: Any, *, max_string_length: int = 2048) -> Any:
    """Recursively redact secrets and bound arbitrary text payloads."""

    if isinstance(value, Mapping):
        return {
            str(key): REDACTED if _is_sensitive_key(key) else redact(item, max_string_length=max_string_length)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple, set)):
        return [redact(item, max_string_length=max_string_length) for item in value]
    if isinstance(value, str) and len(value) > max_string_length:
        return f"{value[:max_string_length]}...[TRUNCATED]"
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


class JsonFormatter(logging.Formatter):
    """Emit one JSON object per line for ingestion by Filebeat or Logstash."""

    def __init__(self, *, service_name: str, environment: str) -> None:
        super().__init__()
        self.service_name = service_name
        self.environment = environment

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, tz=UTC).isoformat().replace("+00:00", "Z"),
            "level": record.levelname,
            "service": self.service_name,
            "environment": self.environment,
            "logger": record.name,
            "message": record.getMessage(),
        }
        context = current_context()
        if context is not None:
            payload.update(context.as_log_fields())

        event = getattr(record, "event", None)
        if event:
            payload["event"] = str(event)
        details = getattr(record, "details", None)
        if details is not None:
            payload["details"] = redact(details)
        if record.exc_info:
            payload["exception"] = self._format_exception(record.exc_info)

        return json.dumps(redact(payload), ensure_ascii=False, separators=(",", ":"))

    def _format_exception(
        self,
        exc_info: tuple[type[BaseException], BaseException, TracebackType | None],
    ) -> dict[str, str]:
        exception_type, exception, _ = exc_info
        return {
            "type": exception_type.__name__,
            "message": str(exception),
            "stacktrace": self.formatException(exc_info),
        }


def configure_logging(
    *,
    service_name: str = "industry-research-api",
    environment: str | None = None,
    level: str | int | None = None,
    log_file: str | Path | None = None,
) -> logging.Logger:
    """Configure idempotent JSON logging for stdout and an optional file."""

    root = logging.getLogger()
    root.setLevel(level or os.getenv("LOG_LEVEL", "INFO").upper())
    formatter = JsonFormatter(
        service_name=service_name,
        environment=environment or os.getenv("ENV", "development"),
    )

    for handler in list(root.handlers):
        if getattr(handler, "_industry_observability", False):
            root.removeHandler(handler)
            handler.close()

    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setFormatter(formatter)
    stream_handler._industry_observability = True  # type: ignore[attr-defined]
    root.addHandler(stream_handler)

    resolved_log_file = log_file or os.getenv("JSON_LOG_FILE")
    if resolved_log_file:
        path = Path(resolved_log_file)
        path.parent.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(
            path,
            maxBytes=int(os.getenv("JSON_LOG_MAX_BYTES", "10485760")),
            backupCount=int(os.getenv("JSON_LOG_BACKUP_COUNT", "5")),
            encoding="utf-8",
        )
        file_handler.setFormatter(formatter)
        file_handler._industry_observability = True  # type: ignore[attr-defined]
        root.addHandler(file_handler)

    return root

