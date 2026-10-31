from types import SimpleNamespace
from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient

from router.auth_router import get_current_user_required
from router.observability_router import get_observability_service, router


class FakeObservabilityService:
    def __init__(self):
        self.calls = []
        self.visible_run = {"run_id": str(uuid4()), "status": "completed"}

    def list_runs(self, **kwargs):
        self.calls.append(("list_runs", kwargs))
        return {"items": [self.visible_run], "next_cursor": None}

    def get_run(self, run_id, *, user_id):
        self.calls.append(("get_run", {"run_id": run_id, "user_id": user_id}))
        return self.visible_run if run_id == self.visible_run["run_id"] else None

    def list_events(self, **kwargs):
        self.calls.append(("list_events", kwargs))
        return {"items": [{"sequence": 1, "event_type": "phase.started"}], "next_cursor": None}

    def get_timeline(self, **kwargs):
        self.calls.append(("get_timeline", kwargs))
        return {"runs": [self.visible_run], "events": [], "next_cursor": None}


def create_client():
    service = FakeObservabilityService()
    user = SimpleNamespace(id=uuid4())
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_current_user_required] = lambda: user
    app.dependency_overrides[get_observability_service] = lambda: service
    return TestClient(app), service, user


def test_list_runs_is_scoped_to_authenticated_user():
    client, service, user = create_client()
    session_id = str(uuid4())

    with client:
        response = client.get("/research/runs", params={"session_id": session_id, "limit": 25})

    assert response.status_code == 200
    assert response.json()["items"][0]["status"] == "completed"
    assert service.calls[-1] == (
        "list_runs",
        {"session_id": session_id, "user_id": str(user.id), "limit": 25},
    )


def test_get_run_and_events_return_owner_scoped_data():
    client, service, user = create_client()
    run_id = service.visible_run["run_id"]

    with client:
        run_response = client.get(f"/research/runs/{run_id}")
        events_response = client.get(
            f"/research/runs/{run_id}/events",
            params={"after_sequence": 0, "limit": 20},
        )

    assert run_response.status_code == 200
    assert events_response.status_code == 200
    assert events_response.json()["items"][0]["event_type"] == "phase.started"
    assert service.calls[-1][1]["user_id"] == str(user.id)


def test_missing_or_other_users_run_is_hidden_as_not_found():
    client, _, _ = create_client()

    with client:
        response = client.get(f"/research/runs/{uuid4()}")

    assert response.status_code == 404


def test_timeline_and_validation_are_bounded():
    client, service, user = create_client()
    session_id = str(uuid4())

    with client:
        timeline = client.get(f"/research/sessions/{session_id}/timeline")
        bad_cursor = client.get(
            f"/research/runs/{service.visible_run['run_id']}/events",
            params={"after_sequence": -1},
        )
        bad_id = client.get("/research/runs/not-a-uuid")
        excessive_limit = client.get(
            "/research/runs",
            params={"session_id": session_id, "limit": 101},
        )

    assert timeline.status_code == 200
    assert service.calls[0] == (
        "get_timeline",
        {"session_id": session_id, "user_id": str(user.id)},
    )
    assert bad_cursor.status_code == 422
    assert bad_id.status_code == 400
    assert excessive_limit.status_code == 422

