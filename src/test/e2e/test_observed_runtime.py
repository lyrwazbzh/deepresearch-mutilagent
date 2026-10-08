from __future__ import annotations

from fastapi.testclient import TestClient

from src.api_view.web_main import create_app
from src.agent.config import Settings
from src.agent.container import build_container
from src.agent.backends.events import InMemoryEventBroker
from src.agent.llm.runtime import ModelRuntime, ProviderChatResponse
from src.agent.workers.outbox import OutboxDispatcher
from src.test.harness import ProviderAttemptSequence, in_memory_telemetry


class CapturingResearchQueue:
    def __init__(self) -> None:
        self.task_ids: list[str] = []

    def publish_research(self, task_id: str) -> None:
        self.task_ids.append(task_id)


def test_phase12_api_outbox_worker_graph_is_observed_through_provider_boundary(
    session_factory,
) -> None:
    telemetry, exporter = in_memory_telemetry(session_factory)
    provider = ProviderAttemptSequence(
        chat=[
            ProviderChatResponse(
                content='{"mode":"direct","target_agent":null}',
                input_tokens=12,
                output_tokens=4,
            ),
            ProviderChatResponse(
                content="deterministic end-to-end answer",
                input_tokens=18,
                output_tokens=7,
            ),
        ]
    )
    runtime = ModelRuntime(
        session_factory,
        provider_factory=lambda _provider: provider,
        telemetry=telemetry,
    )
    settings = Settings(
        database_url="sqlite://",
        redis_url="redis://unused",
        celery_broker_url="redis://unused",
        default_tenant_id="tenant_test",
        default_user_id="user_test",
        model_mode="fake",
        phase2_enabled=False,
    )
    container = build_container(
        settings,
        session_factory=session_factory,
        model_runtime=runtime,
        event_broker=InMemoryEventBroker(),
        telemetry=telemetry,
    )
    client = TestClient(create_app(container))
    created = client.post(
        "/api/v1/tasks",
        headers={"Idempotency-Key": "phase12-observed-e2e"},
        json={"query": "observe this research task"},
    )
    assert created.status_code == 201
    task_id = created.json()["task_id"]

    queue = CapturingResearchQueue()
    dispatcher = OutboxDispatcher(session_factory, queue, InMemoryEventBroker())
    assert dispatcher.dispatch_once() == 1
    assert queue.task_ids == [task_id]
    container.worker.run(queue.task_ids.pop())

    projection = client.get(f"/api/v1/tasks/{task_id}").json()
    assert projection["task_status"] == "completed"
    spans = exporter.get_finished_spans()
    roots = [span for span in spans if span.name == "research.run"]
    nodes = [span for span in spans if span.name == "graph.node"]
    models = [span for span in spans if span.name == "gen_ai.chat"]
    assert len(roots) == 1
    assert [span.attributes["research.graph_node"] for span in nodes] == [
        "route",
        "chief_direct",
    ]
    assert len(models) == 2
    assert all(span.parent is not None for span in [*nodes, *models])
    assert provider.attempts == [
        ("chat", "fake", "test"),
        ("chat", "fake", "test"),
    ]
