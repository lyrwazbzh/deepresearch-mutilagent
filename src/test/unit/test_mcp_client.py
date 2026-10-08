from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import timedelta

from pydantic import BaseModel, ConfigDict, Field

from src.agent.tools.mcp_client import MCPServerConfig, OfficialMCPClient, register_mcp_server
from src.agent.tools.web import SSRFGuard
from src.agent.schema.capability import CapabilitySnapshot
from src.agent.schema.runtime import ExecutionContext
from src.agent.backends.persistence.models import utcnow
from src.agent.runtime.tool_runtime import ToolPolicy, ToolRegistry, ToolRuntime


class Token:
    def is_cancelled(self) -> bool:
        return False


class Credentials:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str | None]] = []

    def resolve(
        self, tenant_id: str, user_id: str, credential_ref: str | None
    ) -> str | None:
        self.calls.append((tenant_id, user_id, credential_ref))
        return "scoped-token" if credential_ref else None


class FakeMCPClient:
    def __init__(self, marker: str) -> None:
        self.marker = marker
        self.calls: list[tuple[str, str | None, str, dict]] = []

    def list_tools(self, endpoint: str, credential: str | None):
        assert endpoint == "https://mcp.example.com/"
        assert credential == "scoped-token"
        return [
            {
                "name": "lookup",
                "description": "Lookup one value",
                "inputSchema": {
                    "type": "object",
                    "properties": {"query": {"type": "string"}},
                    "required": ["query"],
                    "additionalProperties": False,
                },
            }
        ]

    def call_tool(
        self,
        endpoint: str,
        credential: str | None,
        name: str,
        arguments: dict,
    ):
        self.calls.append((endpoint, credential, name, arguments))
        return {
            "structuredContent": {"marker": self.marker, "query": arguments["query"]},
            "summary": "lookup complete",
        }


class OfficialTool(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    name: str
    input_schema: dict = Field(alias="inputSchema")


class OfficialPage(BaseModel):
    tools: list[OfficialTool]
    next_cursor: str | None = None


class OfficialResult(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    structured_content: dict = Field(alias="structuredContent")
    is_error: bool = Field(default=False, alias="isError")


class FakeOfficialSession:
    def __init__(self) -> None:
        self.list_calls: list[str | None] = []

    async def list_tools(self, *, cursor=None, cache_mode="use"):
        assert cache_mode == "bypass"
        self.list_calls.append(cursor)
        return OfficialPage(
            tools=[OfficialTool(name="lookup", inputSchema={"type": "object"})],
            next_cursor=None,
        )

    async def call_tool(self, name: str, arguments: dict):
        return OfficialResult(
            structuredContent={"name": name, "arguments": arguments},
        )


def public_guard() -> SSRFGuard:
    return SSRFGuard(resolver=lambda _host, _port: ["93.184.216.34"])


def execution(snapshot: CapabilitySnapshot) -> ExecutionContext:
    return ExecutionContext(
        tenant_id="tenant",
        user_id="user",
        task_id="task",
        thread_id="thread",
        lease_token="lease",
        cancellation_token=Token(),
        deadline=utcnow() + timedelta(minutes=1),
        capability_snapshot=snapshot.model_dump(mode="json"),
    )


def test_official_mcp_client_normalizes_sdk_models_and_scopes_credentials() -> None:
    session = FakeOfficialSession()
    opened: list[tuple[str, str | None]] = []

    @asynccontextmanager
    async def factory(endpoint: str, credential: str | None):
        opened.append((endpoint, credential))
        yield session

    client = OfficialMCPClient(client_factory=factory)
    assert client.list_tools("https://mcp.example.com/mcp", "token") == [
        {"name": "lookup", "inputSchema": {"type": "object"}}
    ]
    assert client.call_tool(
        "https://mcp.example.com/mcp", "token", "lookup", {"query": "orders"}
    ) == {
        "structuredContent": {"name": "lookup", "arguments": {"query": "orders"}},
        "isError": False,
    }
    assert opened == [
        ("https://mcp.example.com/mcp", "token"),
        ("https://mcp.example.com/mcp", "token"),
    ]


def test_mcp_tools_list_call_normalization_and_frozen_provider_resolution() -> None:
    registry = ToolRegistry()
    credentials = Credentials()
    v1 = FakeMCPClient("v1")
    config_v1 = MCPServerConfig(
        server_id="erp",
        endpoint="https://mcp.example.com",
        credential_ref="ERP_MCP_TOKEN",
        revision="v1",
    )
    assert register_mcp_server(
        registry,
        config=config_v1,
        client=v1,
        credential_resolver=credentials,
        endpoint_guard=public_guard(),
    ) == ["mcp_erp_lookup"]
    frozen = registry.snapshot()
    snapshot = CapabilitySnapshot(
        tools=frozen,
        skills=[],
        policy_revision="policy-v1",
    )

    v2 = FakeMCPClient("v2")
    register_mcp_server(
        registry,
        config=MCPServerConfig(
            server_id="erp",
            endpoint="https://mcp.example.com",
            credential_ref="ERP_MCP_TOKEN",
            revision="v2",
        ),
        client=v2,
        credential_resolver=credentials,
        endpoint_guard=public_guard(),
    )
    result = ToolRuntime(registry).execute(
        agent_name="data_analyst",
        tool_name="mcp_erp_lookup",
        arguments={"query": "orders"},
        execution=execution(snapshot),
        node_run_id="node",
        policy=ToolPolicy(frozenset({"mcp_erp_lookup"})),
    )
    assert result.status == "success"
    assert result.data == {"marker": "v1", "query": "orders"}
    assert result.trace is not None
    assert result.trace.metadata["provider_ref"] == config_v1.provider_ref
    assert len(v1.calls) == 1
    assert not v2.calls
    assert credentials.calls[-1] == ("tenant", "user", "ERP_MCP_TOKEN")


def test_mcp_endpoint_is_rejected_before_credentials_are_resolved() -> None:
    registry = ToolRegistry()
    credentials = Credentials()
    guard = SSRFGuard(resolver=lambda _host, _port: ["127.0.0.1"])
    try:
        register_mcp_server(
            registry,
            config=MCPServerConfig(
                server_id="private",
                endpoint="https://private.example.com",
                credential_ref="PRIVATE_TOKEN",
            ),
            client=FakeMCPClient("private"),
            credential_resolver=credentials,
            endpoint_guard=guard,
        )
    except PermissionError:
        pass
    else:
        raise AssertionError("private MCP endpoint must be rejected")
    assert credentials.calls == []
    assert registry.names() == []


def test_mcp_schema_validation_happens_in_shared_tool_runtime() -> None:
    registry = ToolRegistry()
    credentials = Credentials()
    register_mcp_server(
        registry,
        config=MCPServerConfig(
            server_id="erp",
            endpoint="https://mcp.example.com",
            credential_ref="ERP_MCP_TOKEN",
        ),
        client=FakeMCPClient("v1"),
        credential_resolver=credentials,
        endpoint_guard=public_guard(),
    )
    snapshot = CapabilitySnapshot(
        tools=registry.snapshot(),
        skills=[],
        policy_revision="policy-v1",
    )
    result = ToolRuntime(registry).execute(
        agent_name="data_analyst",
        tool_name="mcp_erp_lookup",
        arguments={"unexpected": True},
        execution=execution(snapshot),
        node_run_id="node",
        policy=ToolPolicy(frozenset({"mcp_erp_lookup"})),
    )
    assert result.status == "error"
    assert result.error is not None and result.error.code == "schema_validation"
