from contextlib import contextmanager

from observability.context import ObservabilityContext, bind_context, current_context
from observability.events import (
    add_run_usage,
    bind_event_recorder,
    bind_run_usage,
    record_research_event,
)
from observability.tracing import TracingManager


class FakeObservation:
    def __init__(self, name: str, index: int):
        self.name = name
        self.trace_id = "trace-1"
        self.id = f"span-{index}"
        self.updates = []

    def update(self, **kwargs):
        self.updates.append(kwargs)
        return self


class FakeClient:
    def __init__(self):
        self.starts = []
        self.observations = []
        self.flushed = False
        self.stopped = False

    @contextmanager
    def start_as_current_observation(self, **kwargs):
        self.starts.append(kwargs)
        observation = FakeObservation(kwargs["name"], len(self.starts))
        self.observations.append(observation)
        yield observation

    def flush(self):
        self.flushed = True

    def shutdown(self):
        self.stopped = True


def test_disabled_tracing_is_a_noop():
    manager = TracingManager.disabled()

    with manager.span("research.run") as observation:
        assert observation.enabled is False
        observation.update(output={"result": "ignored"})
        observation.record_usage(input_tokens=2, output_tokens=3)


def test_nested_spans_bind_trace_and_span_ids_then_restore_context():
    client = FakeClient()
    manager = TracingManager(client=client, enabled=True, capture_content=False)
    root = ObservabilityContext(request_id="req-1", session_id="session-1")

    with bind_context(root):
        with manager.span("research.run", kind="agent") as parent:
            assert parent.enabled is True
            assert current_context().trace_id == "trace-1"
            assert current_context().span_id == "span-1"
            with manager.span("research.phase", kind="chain"):
                assert current_context().trace_id == "trace-1"
                assert current_context().span_id == "span-2"
            assert current_context().span_id == "span-1"
        assert current_context() == root

    assert [item["as_type"] for item in client.starts] == ["agent", "chain"]
    assert client.starts[0]["metadata"]["request_id"] == "req-1"
    assert client.starts[0]["metadata"]["session_id"] == "session-1"


def test_generation_summarizes_input_and_records_usage():
    client = FakeClient()
    manager = TracingManager(client=client, enabled=True, capture_content=False)

    with manager.generation(
        "llm.outline",
        model="qwen-plus",
        input_summary={"prompt": "private prompt text"},
    ) as generation:
        generation.record_usage(input_tokens=120, output_tokens=40)
        generation.update(output={"response": "private response"})

    started = client.starts[0]
    assert started["as_type"] == "generation"
    assert started["model"] == "qwen-plus"
    assert started["input"]["prompt"]["redacted"] is True
    assert client.observations[0].updates[0]["usage_details"] == {
        "input": 120,
        "output": 40,
        "total": 160,
    }
    assert client.observations[0].updates[1]["output"]["response"]["redacted"] is True


def test_initialization_failure_returns_disabled_manager():
    def broken_factory(**_kwargs):
        raise RuntimeError("invalid exporter configuration")

    manager = TracingManager.from_environment(
        environ={
            "OBSERVABILITY_TRACING_ENABLED": "true",
            "LANGFUSE_PUBLIC_KEY": "pk-test",
            "LANGFUSE_SECRET_KEY": "sk-test",
            "LANGFUSE_BASE_URL": "http://localhost:3000",
        },
        client_factory=broken_factory,
    )

    assert manager.enabled is False


def test_shutdown_flushes_and_stops_client():
    client = FakeClient()
    manager = TracingManager(client=client, enabled=True, capture_content=False)

    assert manager.shutdown(timeout_seconds=1.0) is True
    assert client.flushed is True
    assert client.stopped is True


def test_event_recorder_and_usage_are_scoped_to_current_run():
    recorded = []

    with bind_event_recorder(lambda **event: recorded.append(event)):
        with bind_run_usage() as usage:
            record_research_event("llm.completed", phase="planning", payload={"input_tokens": 3})
            add_run_usage(input_tokens=3, output_tokens=2, cost=0.01)
            add_run_usage(input_tokens=4, output_tokens=1, cost=0.02)

    assert recorded == [
        {
            "event_type": "llm.completed",
            "phase": "planning",
            "status": "info",
            "payload": {"input_tokens": 3},
            "duration_ms": None,
        }
    ]
    assert usage.input_tokens == 7
    assert usage.output_tokens == 3
    assert usage.estimated_cost == 0.03
