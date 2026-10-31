import importlib
import json
import sys
import types
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from pydantic import ValidationError


def _install_router_dependency_stubs():
    service_package = sys.modules["service"]

    class DummyResearchService:
        def __init__(self, **_kwargs):
            pass

    class DummyServiceConfig:
        @staticmethod
        def get_api_config():
            return {}

    service_package.ResearchService = DummyResearchService
    service_package.ServiceConfig = DummyServiceConfig

    dr_g = types.ModuleType("service.dr_g")
    dr_g.serialize_event = lambda event: json.dumps(event, ensure_ascii=False)
    sys.modules["service.dr_g"] = dr_g

    redis_client = types.ModuleType("core.redis_client")
    redis_client.cache = SimpleNamespace(
        set=lambda *_args, **_kwargs: None,
        get=lambda *_args, **_kwargs: None,
        delete=lambda *_args, **_kwargs: None,
    )
    sys.modules["core.redis_client"] = redis_client

    v2_service = types.ModuleType("service.deep_research_v2.service")
    v2_service.DeepResearchV2Service = object
    sys.modules["service.deep_research_v2.service"] = v2_service

    auth_router = types.ModuleType("router.auth_router")

    def require_user():
        raise HTTPException(status_code=401, detail="Not authenticated")

    auth_router.get_current_user_required = require_user
    sys.modules["router.auth_router"] = auth_router


@pytest.fixture
def research_router():
    _install_router_dependency_stubs()
    sys.modules.pop("router.research_router", None)
    return importlib.import_module("router.research_router")


def _sections(count=3):
    return [
        {
            "id": f"section_{index}",
            "title": f"章节 {index}",
            "description": f"描述 {index}",
        }
        for index in range(1, count + 1)
    ]


def _questions(count=3):
    return [
        {"id": f"question_{index}", "text": f"问题 {index}"}
        for index in range(1, count + 1)
    ]


def _approval_request(research_router):
    return research_router.ApproveOutlineRequest(
        outline_revision="revision-1",
        sections=_sections(),
        research_questions=_questions(),
    )


def _user():
    return SimpleNamespace(id="11111111-1111-1111-1111-111111111111")


def test_stream_requires_authentication(research_router):
    app = FastAPI()
    app.include_router(research_router.router)

    with TestClient(app) as client:
        response = client.post(
            "/research/stream",
            json={"query": "industry research", "version": "v2"},
        )

    assert response.status_code == 401


@pytest.mark.asyncio
async def test_stream_passes_current_user_id_to_v2(research_router, monkeypatch):
    research_calls = []

    class FakeV2Service:
        async def research(self, **kwargs):
            research_calls.append(kwargs)
            yield 'data: {"type":"outline_ready"}\n\n'

    monkeypatch.setattr(
        research_router,
        "get_research_service_v2",
        lambda: FakeV2Service(),
    )
    response = await research_router.stream_research(
        # 一律用关键字：T64 把 `current_user` 挪到了 `services` **之前**（见
        # `test_router_auth.py::test_auth_is_the_first_dependency_resolved`），
        # 位置参数会随签名顺序变动而静默错位。关键字参数与顺序解耦。
        request=research_router.ResearchRequest(query="industry research", version="v2"),
        services={"research_service": object()},
        current_user=_user(),
    )
    _ = [chunk async for chunk in response.body_iterator]

    assert research_calls[0]["user_id"] == str(_user().id)


def test_approve_request_enforces_three_to_twelve_items(research_router):
    request_model = research_router.ApproveOutlineRequest

    valid = request_model(
        outline_revision="revision-1",
        sections=_sections(),
        research_questions=_questions(),
    )
    assert len(valid.sections) == 3

    with pytest.raises(ValidationError):
        request_model(
            outline_revision="revision-1",
            sections=_sections(2),
            research_questions=_questions(),
        )


@pytest.mark.asyncio
async def test_approve_outline_returns_resumed_sse_for_owner(
    research_router, monkeypatch
):
    approved_calls = []
    research_calls = []

    class FakeCheckpointService:
        def approve_outline(self, **kwargs):
            approved_calls.append(kwargs)
            return {"query": "产业研究", "phase": "planning"}

    class FakeV2Service:
        async def research(self, **kwargs):
            research_calls.append(kwargs)
            yield "data: {\"type\":\"phase\",\"phase\":\"planning\"}\n\n"

    monkeypatch.setattr(
        research_router,
        "get_checkpoint_service",
        lambda: FakeCheckpointService(),
    )
    monkeypatch.setattr(
        research_router,
        "get_research_service_v2",
        lambda: FakeV2Service(),
    )
    request = _approval_request(research_router)
    user = _user()

    response = await research_router.approve_research_outline(
        "session-1", request, user
    )
    chunks = [chunk async for chunk in response.body_iterator]
    body = "".join(
        chunk.decode("utf-8") if isinstance(chunk, bytes) else chunk
        for chunk in chunks
    )

    assert response.media_type == "text/event-stream"
    assert '"phase":"planning"' in body
    assert approved_calls[0]["user_id"] == str(user.id)
    assert research_calls[0]["resume"] is True
    assert research_calls[0]["user_id"] == str(user.id)


@pytest.mark.asyncio
async def test_approve_outline_maps_conflict_to_409(research_router, monkeypatch):
    from service.checkpoint_service import OutlineApprovalConflict

    class FakeCheckpointService:
        def approve_outline(self, **_kwargs):
            raise OutlineApprovalConflict("stale")

    monkeypatch.setattr(
        research_router,
        "get_checkpoint_service",
        lambda: FakeCheckpointService(),
    )
    request = _approval_request(research_router)

    with pytest.raises(research_router.HTTPException) as caught:
        await research_router.approve_research_outline(
            "session-1",
            request,
            _user(),
        )

    assert caught.value.status_code == 409


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["missing", "foreign"])
async def test_approve_maps_missing_or_foreign_checkpoint_to_identical_404(
    research_router,
    monkeypatch,
    case,
):
    from service.checkpoint_service import CheckpointNotFound

    class FakeCheckpointService:
        def approve_outline(self, **_kwargs):
            raise CheckpointNotFound("Checkpoint not found")

    service_created = False

    def create_v2_service():
        nonlocal service_created
        service_created = True
        return object()

    monkeypatch.setattr(
        research_router,
        "get_checkpoint_service",
        lambda: FakeCheckpointService(),
    )
    monkeypatch.setattr(
        research_router,
        "get_research_service_v2",
        create_v2_service,
    )

    with pytest.raises(research_router.HTTPException) as caught:
        await research_router.approve_research_outline(
            f"session-{case}",
            _approval_request(research_router),
            _user(),
        )

    assert caught.value.status_code == 404
    assert caught.value.detail == "Checkpoint not found"
    assert service_created is False


@pytest.mark.asyncio
async def test_resume_rejects_awaiting_approval_with_409(
    research_router,
    monkeypatch,
):
    class FakeCheckpointService:
        def get_checkpoint_info(self, session_id, user_id=None):
            return {
                "session_id": session_id,
                "user_id": user_id,
                "query": "industry research",
                "status": "paused",
                "phase": "awaiting_outline_approval",
            }

    monkeypatch.setattr(
        research_router,
        "get_checkpoint_service",
        lambda: FakeCheckpointService(),
    )

    with pytest.raises(research_router.HTTPException) as caught:
        await research_router.resume_research("session-1", _user())

    assert caught.value.status_code == 409
    assert "approval required" in caught.value.detail.lower()


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["cancel", "read", "delete"])
async def test_cancel_read_and_delete_are_user_scoped(
    research_router,
    monkeypatch,
    operation,
):
    calls = []

    class FakeCheckpointService:
        def get_checkpoint_info(self, session_id, user_id=None):
            calls.append(("read", session_id, user_id))
            return None

        def delete_checkpoint(self, session_id, user_id=None):
            calls.append(("delete", session_id, user_id))
            return False

    monkeypatch.setattr(
        research_router,
        "get_checkpoint_service",
        lambda: FakeCheckpointService(),
    )
    endpoint = {
        "cancel": research_router.cancel_research,
        "read": research_router.get_checkpoint,
        "delete": research_router.delete_checkpoint,
    }[operation]

    with pytest.raises(research_router.HTTPException) as caught:
        await endpoint("foreign-session", _user())

    assert caught.value.status_code == 404
    assert calls[0][2] == str(_user().id)
