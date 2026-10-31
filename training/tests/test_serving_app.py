from fastapi.testclient import TestClient

from src.contracts.tool_routing import ToolCall, ToolRoutingResponse
from src.serve.app import create_app


class FakeEngine:
    def model_info(self):
        return {"model_version": "fake", "adapter_version": "v1", "schema_version": "1.0"}

    async def route(self, request):
        return ToolRoutingResponse(
            decision_id="decision-1",
            calls=[ToolCall(call_id="call-1", name=request.allowed_tools[0], arguments={"query": request.query})],
            confidence=0.9,
            model_version="fake",
            adapter_version="v1",
        )


def test_versioned_routing_endpoint(monkeypatch):
    monkeypatch.delenv("TOOL_ROUTER_API_TOKEN", raising=False)
    with TestClient(create_app(FakeEngine())) as client:
        response = client.post("/v1/tool-routing", json={
            "run_id": "r1", "session_id": "s1", "stage": "research",
            "query": "储能市场", "allowed_tools": ["web_search"],
            "capabilities": {"web_enabled": True},
        })
        assert response.status_code == 200
        assert response.json()["calls"][0]["name"] == "web_search"
