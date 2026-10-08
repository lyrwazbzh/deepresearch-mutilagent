from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from src.api_view.web_main import create_app
from src.agent.task_service import CreateTaskRequest


def test_task_api_projection_and_problem_details(make_container) -> None:
    container, _ = make_container({})
    client = TestClient(create_app(container))
    response = client.post(
        "/api/v1/tasks",
        headers={"Idempotency-Key": "api-key"},
        json={"query": "hello", "options": {"max_depth": 8}},
    )
    assert response.status_code == 201
    body = response.json()
    assert body["task_status"] == "pending"
    assert body["phase"] == "routing"
    assert "latest_state" not in body
    get_response = client.get(f"/api/v1/tasks/{body['task_id']}")
    assert get_response.status_code == 200

    conflict = client.post(
        "/api/v1/tasks",
        headers={"Idempotency-Key": "api-key"},
        json={"query": "different"},
    )
    assert conflict.status_code == 409
    assert conflict.headers["content-type"].startswith("application/problem+json")
    assert conflict.json()["code"] == "idempotency_conflict"


def test_sse_endpoint_honors_last_event_id_and_after_id(make_container) -> None:
    container, _ = make_container({})
    created = container.task_service.create(
        "tenant_test", "user_test", "sse-cursor", CreateTaskRequest(query="hello")
    )
    seen: list[int] = []

    async def finite_stream(tenant_id: str, task_id: str, after_id: int):
        assert tenant_id == "tenant_test"
        assert task_id == created.projection.task_id
        seen.append(after_id)
        yield "id: 18\nevent: task_updated\ndata: {}\n\n"

    container.event_service.stream = finite_stream  # type: ignore[method-assign]
    client = TestClient(create_app(container))
    response = client.get(
        f"/api/v1/tasks/{created.projection.task_id}/events",
        headers={"Last-Event-ID": "17"},
    )
    assert response.status_code == 200
    assert "id: 18" in response.text
    response = client.get(
        f"/api/v1/tasks/{created.projection.task_id}/events?after_id=21",
        headers={"Last-Event-ID": "17"},
    )
    assert response.status_code == 200
    assert seen == [17, 21]


@pytest.mark.asyncio
async def test_sse_replay_and_live_are_backed_by_committed_events(make_container) -> None:
    container, _ = make_container(
        {"routing": [{"mode": "direct"}], "reasoning": ["answer"]}
    )
    created = container.task_service.create(
        "tenant_test", "user_test", "event-key", CreateTaskRequest(query="hello")
    )
    task_id = created.projection.task_id
    container.worker.run(task_id)
    replay = container.event_service.replay("tenant_test", task_id, 0)
    assert len(replay) >= 3
    event_ids = [int(item["event_id"]) for item in replay]
    assert event_ids == sorted(event_ids)
    assert len(event_ids) == len(set(event_ids))
    after = container.event_service.replay("tenant_test", task_id, event_ids[0])
    assert [int(item["event_id"]) for item in after] == event_ids[1:]

    broker = container.event_service.broker
    async with broker.subscribe("tenant_test", task_id) as notifications:
        await broker.publish("tenant_test", task_id, event_ids[-1])
        assert await anext(notifications) == event_ids[-1]
