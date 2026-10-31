import json
import logging

from observability.context import ObservabilityContext, bind_context
from observability.logging import JsonFormatter, redact


def test_redact_recursively_sanitizes_secrets_and_long_values():
    payload = {
        "authorization": "Bearer super-secret",
        "nested": {
            "password": "do-not-log",
            "input_tokens": 321,
            "items": [{"api_key": "key-value"}],
        },
        "large_text": "x" * 30,
    }

    sanitized = redact(payload, max_string_length=12)

    assert sanitized["authorization"] == "[REDACTED]"
    assert sanitized["nested"]["password"] == "[REDACTED]"
    assert sanitized["nested"]["items"][0]["api_key"] == "[REDACTED]"
    assert sanitized["nested"]["input_tokens"] == 321
    assert sanitized["large_text"] == "xxxxxxxxxxxx...[TRUNCATED]"


def test_json_formatter_injects_correlation_context_and_structured_fields():
    formatter = JsonFormatter(service_name="research-api", environment="test")
    record = logging.LogRecord(
        name="test.logger",
        level=logging.INFO,
        pathname=__file__,
        lineno=42,
        msg="research started",
        args=(),
        exc_info=None,
    )
    record.event = "research.started"
    record.details = {"session_id": "must-not-override", "api_key": "secret"}

    context = ObservabilityContext(
        request_id="req-1",
        session_id="session-1",
        research_id="research-1",
        run_id="run-1",
        trace_id="trace-1",
        span_id="span-1",
    )
    with bind_context(context):
        output = json.loads(formatter.format(record))

    assert output["service"] == "research-api"
    assert output["environment"] == "test"
    assert output["level"] == "INFO"
    assert output["event"] == "research.started"
    assert output["request_id"] == "req-1"
    assert output["session_id"] == "session-1"
    assert output["research_id"] == "research-1"
    assert output["run_id"] == "run-1"
    assert output["trace_id"] == "trace-1"
    assert output["span_id"] == "span-1"
    assert output["details"]["api_key"] == "[REDACTED]"
    assert output["details"]["session_id"] == "must-not-override"
    assert output["timestamp"].endswith("Z")


def test_json_formatter_serializes_exception_without_losing_message():
    formatter = JsonFormatter(service_name="research-api", environment="test")
    try:
        raise ValueError("bad request")
    except ValueError:
        record = logging.LogRecord(
            name="test.logger",
            level=logging.ERROR,
            pathname=__file__,
            lineno=80,
            msg="request failed",
            args=(),
            exc_info=__import__("sys").exc_info(),
        )

    output = json.loads(formatter.format(record))

    assert output["message"] == "request failed"
    assert output["exception"]["type"] == "ValueError"
    assert output["exception"]["message"] == "bad request"
    assert "ValueError: bad request" in output["exception"]["stacktrace"]

