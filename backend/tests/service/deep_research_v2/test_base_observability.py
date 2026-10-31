from contextlib import contextmanager
from types import SimpleNamespace

from observability.events import bind_event_recorder, bind_run_usage
from service.deep_research_v2.agents import base as base_module
from service.deep_research_v2.agents.base import BaseAgent


class ConcreteAgent(BaseAgent):
    async def process(self, state):
        return state


class FakeTraceObservation:
    def __init__(self):
        self.usage = None
        self.updates = []

    def record_usage(self, **usage):
        self.usage = usage

    def update(self, **fields):
        self.updates.append(fields)


async def test_call_llm_records_generation_tokens_metrics_and_run_event(monkeypatch):
    trace = FakeTraceObservation()

    @contextmanager
    def fake_generation(*_args, **_kwargs):
        yield trace

    monkeypatch.setattr(base_module, "generation", fake_generation)
    agent = ConcreteAgent(
        name="Architect",
        role="planner",
        llm_api_key="test-key",
        llm_base_url="http://example.invalid/v1",
        model="qwen-plus",
    )
    response = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content='{"ok": true}'))],
        usage=SimpleNamespace(prompt_tokens=120, completion_tokens=30),
    )
    agent.client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=lambda **_kwargs: response))
    )
    events = []

    with bind_event_recorder(lambda **event: events.append(event)):
        with bind_run_usage() as usage:
            result = await agent.call_llm("system secret", "user secret")

    assert result == '{"ok": true}'
    assert trace.usage == {"input_tokens": 120, "output_tokens": 30}
    assert trace.updates == [{"output": {"response_length": 12}}]
    assert usage.input_tokens == 120
    assert usage.output_tokens == 30
    assert events[0]["event_type"] == "llm.completed"
    assert events[0]["payload"] == {
        "agent": "Architect",
        "model": "qwen-plus",
        "input_tokens": 120,
        "output_tokens": 30,
        "response_length": 12,
    }

