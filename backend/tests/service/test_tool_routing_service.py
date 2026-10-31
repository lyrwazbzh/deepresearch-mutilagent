import asyncio

from contracts.tool_routing import CapabilitySet, ToolName, ToolRoutingRequest
from service.tool_routing.fallback import DeterministicToolRouter
from service.tool_routing.service import ToolRoutingService


def request(session_id="session"):
    return ToolRoutingRequest(
        run_id="run",
        session_id=session_id,
        stage="research",
        query="电池价格趋势",
        allowed_tools=[ToolName.WEB_SEARCH, ToolName.LOCAL_RAG],
        capabilities=CapabilitySet(web_enabled=True, kb_enabled=True, kb_name="auto"),
    )


def test_deterministic_router_expands_enabled_sources():
    response = asyncio.run(DeterministicToolRouter().route(request()))
    assert [call.name for call in response.calls] == [
        ToolName.WEB_SEARCH,
        ToolName.LOCAL_RAG,
    ]


def test_gray_assignment_is_stable():
    router = ToolRoutingService(
        fallback=DeterministicToolRouter(),
        adapter=DeterministicToolRouter(),
        gray_percent=50,
    )
    assert router.adapter_selected(request("same")) == router.adapter_selected(request("same"))
