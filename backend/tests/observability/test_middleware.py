from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from prometheus_client import CollectorRegistry

from observability.context import current_context
from observability.metrics import ApplicationMetrics, metrics_response
from observability.middleware import ObservabilityMiddleware


def create_test_app() -> tuple[FastAPI, ApplicationMetrics]:
    registry = CollectorRegistry()
    metrics = ApplicationMetrics(registry=registry)
    app = FastAPI()
    app.add_middleware(ObservabilityMiddleware, metrics=metrics)

    @app.get("/items/{item_id}")
    async def read_item(item_id: str, request: Request):
        context = current_context()
        assert context is not None
        return {
            "item_id": item_id,
            "request_id": context.request_id,
            "session_id": context.session_id,
        }

    @app.get("/metrics")
    async def read_metrics():
        return metrics_response(registry)

    return app, metrics


def test_middleware_propagates_ids_and_emits_bounded_route_metrics():
    app, _ = create_test_app()

    with TestClient(app) as client:
        response = client.get(
            "/items/abc-123",
            headers={"X-Request-ID": "req-client", "X-Session-ID": "session-9"},
        )
        metrics = client.get("/metrics").text

    assert response.status_code == 200
    assert response.json()["request_id"] == "req-client"
    assert response.json()["session_id"] == "session-9"
    assert response.headers["X-Request-ID"] == "req-client"
    assert 'route="/items/{item_id}"' in metrics
    assert 'method="GET"' in metrics
    assert 'status="200"' in metrics
    assert "abc-123" not in metrics


def test_middleware_replaces_unsafe_request_id():
    app, _ = create_test_app()

    with TestClient(app) as client:
        response = client.get("/items/1", headers={"X-Request-ID": "unsafe request id"})

    assert response.status_code == 200
    assert response.headers["X-Request-ID"].startswith("req-")
    assert response.headers["X-Request-ID"] != "unsafe request id"

