from __future__ import annotations

from fastapi.testclient import TestClient

from src.api_view.web_main import create_app
from src.agent.config import Settings
from src.agent.container import build_container
from src.agent.backends.events import InMemoryEventBroker
from src.agent.task_service import CreateTaskRequest
from src.agent.workers.outbox import OutboxDispatcher


class CapturingCeleryBoundary:
    def __init__(self) -> None:
        self.tasks: list[str] = []

    def publish_research(self, task_id: str) -> None:
        self.tasks.append(task_id)


def test_phase1_direct_http_outbox_worker_projection_and_event_replay(
    make_container, session_factory
) -> None:
    container, _ = make_container(
        {"routing": [{"mode": "direct"}], "reasoning": ["end-to-end answer"]}
    )
    client = TestClient(create_app(container))
    created = client.post(
        "/api/v1/tasks",
        headers={"Idempotency-Key": "phase1-e2e"},
        json={"query": "hello"},
    )
    assert created.status_code == 201
    task_id = created.json()["task_id"]

    celery_boundary = CapturingCeleryBoundary()
    dispatcher = OutboxDispatcher(session_factory, celery_boundary, InMemoryEventBroker())
    assert dispatcher.dispatch_once() == 1
    assert celery_boundary.tasks == [task_id]

    container.worker.run(celery_boundary.tasks.pop())
    projection = client.get(f"/api/v1/tasks/{task_id}").json()
    assert projection["task_status"] == "completed"
    assert projection["phase"] == "completed"
    events = container.event_service.replay("tenant_test", task_id, 0)
    assert events[-1]["data"]["changed"] == ["task_status", "phase"]


def test_local_development_provider_runs_without_network(session_factory) -> None:
    settings = Settings(
        database_url="sqlite://",
        redis_url="redis://unused",
        celery_broker_url="redis://unused",
        default_tenant_id="tenant_test",
        default_user_id="user_test",
        model_mode="fake",
    )
    container = build_container(
        settings,
        session_factory=session_factory,
        event_broker=InMemoryEventBroker(),
    )
    created = container.task_service.create(
        "tenant_test",
        "user_test",
        "real-runtime-fake-provider",
        CreateTaskRequest(query="hello"),
    )
    container.worker.run(created.projection.task_id)
    projection = container.task_service.get("tenant_test", "user_test", created.projection.task_id)
    assert projection.task_status.value == "completed"
