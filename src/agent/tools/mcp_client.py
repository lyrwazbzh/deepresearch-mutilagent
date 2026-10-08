from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, AsyncContextManager, Callable, Protocol

from pydantic import BaseModel, ConfigDict, create_model

from src.agent.tools.web import SSRFGuard
from src.agent.identity import canonical_json
from src.agent.runtime.tool_runtime import (
    ToolDefinition,
    ToolError,
    ToolExecutionContext,
    ToolRegistry,
    ToolResult,
)


class MCPClient(Protocol):
    """Boundary implemented by the official MCP client in production and a fake in tests."""

    def list_tools(self, endpoint: str, credential: str | None) -> list[dict[str, Any]]: ...

    def call_tool(
        self,
        endpoint: str,
        credential: str | None,
        name: str,
        arguments: dict[str, Any],
    ) -> dict[str, Any] | ToolResult: ...


class OfficialMCPClient:
    """Synchronous ToolRuntime boundary backed by the official MCP Python SDK.

    The application ToolRuntime is deliberately synchronous. Each operation therefore owns a
    short-lived SDK connection and runs it to completion here, outside of Agent code. Tests can
    inject a client context factory without importing the optional SDK dependency.
    """

    def __init__(
        self,
        *,
        timeout_seconds: float = 30.0,
        read_timeout_seconds: float = 300.0,
        client_factory: Callable[
            [str, str | None], AsyncContextManager[Any]
        ]
        | None = None,
    ) -> None:
        self.timeout_seconds = timeout_seconds
        self.read_timeout_seconds = read_timeout_seconds
        self._client_factory = client_factory

    def list_tools(self, endpoint: str, credential: str | None) -> list[dict[str, Any]]:
        return self._run(self._list_tools(endpoint, credential))

    def call_tool(
        self,
        endpoint: str,
        credential: str | None,
        name: str,
        arguments: dict[str, Any],
    ) -> dict[str, Any]:
        return self._run(self._call_tool(endpoint, credential, name, arguments))

    async def _list_tools(
        self, endpoint: str, credential: str | None
    ) -> list[dict[str, Any]]:
        tools: list[dict[str, Any]] = []
        async with self._client(endpoint, credential) as client:
            cursor: str | None = None
            while True:
                page = await client.list_tools(cursor=cursor, cache_mode="bypass")
                tools.extend(_model_json(tool) for tool in page.tools)
                cursor = page.next_cursor
                if cursor is None:
                    return tools

    async def _call_tool(
        self,
        endpoint: str,
        credential: str | None,
        name: str,
        arguments: dict[str, Any],
    ) -> dict[str, Any]:
        async with self._client(endpoint, credential) as client:
            # Listing first lets the official client apply schema/header metadata consistently.
            await client.list_tools(cache_mode="bypass")
            result = await client.call_tool(name, arguments)
            return _model_json(result)

    def _client(
        self, endpoint: str, credential: str | None
    ) -> AsyncContextManager[Any]:
        if self._client_factory is not None:
            return self._client_factory(endpoint, credential)
        return self._official_client(endpoint, credential)

    @asynccontextmanager
    async def _official_client(self, endpoint: str, credential: str | None):
        try:
            import httpx2  # type: ignore[import-not-found]
            from mcp import Client  # type: ignore[import-not-found]
            from mcp.client.streamable_http import (  # type: ignore[import-not-found]
                streamable_http_client,
            )
        except ImportError as exc:  # pragma: no cover - optional deployment extra
            raise RuntimeError(
                "Official MCP client is unavailable; install the project with the 'mcp' extra"
            ) from exc

        headers: dict[str, str] = {}
        if credential:
            headers["Authorization"] = (
                credential
                if credential.lower().startswith("bearer ")
                else f"Bearer {credential}"
            )
        timeout = httpx2.Timeout(self.timeout_seconds, read=self.read_timeout_seconds)
        async with httpx2.AsyncClient(headers=headers, timeout=timeout) as http_client:
            transport = streamable_http_client(endpoint, http_client=http_client)
            async with Client(transport, cache=None) as client:
                yield client

    @staticmethod
    def _run(coroutine):
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(coroutine)
        coroutine.close()
        raise RuntimeError(
            "OfficialMCPClient must run at the synchronous ToolRuntime boundary, not on an "
            "active event loop"
        )


class MCPCredentialResolver(Protocol):
    def resolve(
        self, tenant_id: str, user_id: str, credential_ref: str | None
    ) -> str | None: ...


class EnvironmentMCPCredentialResolver:
    """Development resolver; scope is explicit at the boundary and secrets are never snapshotted."""

    def resolve(
        self, tenant_id: str, user_id: str, credential_ref: str | None
    ) -> str | None:
        del tenant_id, user_id
        if credential_ref is None:
            return None
        value = os.getenv(credential_ref)
        if not value:
            raise PermissionError("MCP credential is unavailable")
        return value


@dataclass(frozen=True)
class MCPServerConfig:
    server_id: str
    endpoint: str
    credential_ref: str | None = None
    revision: str = "v1"

    @property
    def provider_ref(self) -> str:
        digest = hashlib.sha256(
            canonical_json(
                {
                    "server_id": self.server_id,
                    "endpoint": self.endpoint,
                    "credential_ref": self.credential_ref,
                    "revision": self.revision,
                }
            )
        ).hexdigest()
        return f"mcp:{self.server_id}:{digest}"


class MCPToolAdapter:
    def __init__(
        self,
        *,
        config: MCPServerConfig,
        remote_name: str,
        client: MCPClient,
        credential_resolver: MCPCredentialResolver,
        endpoint_guard: SSRFGuard,
    ) -> None:
        self.config = config
        self.remote_name = remote_name
        self.client = client
        self.credential_resolver = credential_resolver
        self.endpoint_guard = endpoint_guard

    def execute(
        self, context: ToolExecutionContext, normalized_arguments: dict[str, Any]
    ) -> ToolResult:
        endpoint = self.endpoint_guard.validate(self.config.endpoint)
        credential = self.credential_resolver.resolve(
            context.tenant_id,
            context.user_id,
            self.config.credential_ref,
        )
        raw = self.client.call_tool(
            endpoint,
            credential,
            self.remote_name,
            normalized_arguments,
        )
        if isinstance(raw, ToolResult):
            return raw
        if raw.get("isError"):
            return ToolResult(
                status="error",
                error=ToolError(
                    type="tool_error",
                    code="mcp_tool_error",
                    message="MCP tool returned an error",
                ),
            )
        structured = raw.get("structuredContent")
        if isinstance(structured, dict):
            data = structured
        else:
            data = {"content": raw.get("content", [])}
        return ToolResult(
            status="success",
            summary=str(raw.get("summary") or "MCP tool completed"),
            data=data,
        )


def register_mcp_server(
    registry: ToolRegistry,
    *,
    config: MCPServerConfig,
    client: MCPClient,
    credential_resolver: MCPCredentialResolver,
    endpoint_guard: SSRFGuard | None = None,
) -> list[str]:
    """Normalize tools/list into immutable Tool contracts executed by the shared ToolRuntime."""

    guard = endpoint_guard or SSRFGuard()
    endpoint = guard.validate(config.endpoint)
    # Endpoint authorization always precedes credential resolution.
    credential = credential_resolver.resolve("registry", "registry", config.credential_ref)
    registered: list[str] = []
    for descriptor in client.list_tools(endpoint, credential):
        remote_name = str(descriptor["name"])
        schema = descriptor.get("inputSchema") or {"type": "object", "properties": {}}
        if not isinstance(schema, dict):
            raise ValueError("MCP inputSchema must be a JSON object")
        local_name = _registry_name(config.server_id, remote_name)
        definition = ToolDefinition(
            name=local_name,
            args_schema=_arguments_model(local_name, schema),
            adapter=MCPToolAdapter(
                config=config,
                remote_name=remote_name,
                client=client,
                credential_resolver=credential_resolver,
                endpoint_guard=guard,
            ),
            provider_ref=config.provider_ref,
            schema_json=schema,
        )
        if registry.get(local_name) is None:
            registry.register(definition)
        else:
            registry.replace(definition)
        registered.append(local_name)
    return registered


def _registry_name(server_id: str, remote_name: str) -> str:
    def normalize(value: str) -> str:
        return re.sub(r"[^a-zA-Z0-9_-]+", "_", value).strip("_")

    server = normalize(server_id)
    tool = normalize(remote_name)
    if not server or not tool:
        raise ValueError("MCP server and tool names must contain a stable identifier")
    return f"mcp_{server}_{tool}"


def _model_json(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    dump = getattr(value, "model_dump", None)
    if dump is None:
        raise TypeError("Official MCP SDK returned a non-serializable result")
    rendered = dump(mode="json", by_alias=True)
    if not isinstance(rendered, dict):
        raise TypeError("Official MCP SDK result must serialize to a JSON object")
    return rendered


def _arguments_model(name: str, schema: dict[str, Any]) -> type[BaseModel]:
    if schema.get("type", "object") != "object":
        raise ValueError("MCP Tool inputSchema root must be an object")
    required = set(schema.get("required") or [])
    fields: dict[str, tuple[Any, Any]] = {}
    for field_name, definition in (schema.get("properties") or {}).items():
        if not isinstance(definition, dict):
            raise ValueError("MCP Tool property schema must be an object")
        kind = str(definition.get("type") or "")
        annotation = {
            "string": str,
            "integer": int,
            "number": float,
            "boolean": bool,
            "array": list[Any],
            "object": dict[str, Any],
        }.get(kind, Any)
        fields[str(field_name)] = (
            annotation,
            ... if field_name in required else None,
        )
    model_name = "MCPArgs_" + hashlib.sha256(
        json.dumps(name, sort_keys=True).encode("utf-8")
    ).hexdigest()[:16]
    return create_model(
        model_name,
        __config__=ConfigDict(extra="forbid"),
        **fields,
    )  # type: ignore[call-overload]
