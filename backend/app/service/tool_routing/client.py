"""HTTP client for the QLoRA tool-routing service."""

from __future__ import annotations

import httpx
try:
    from opentelemetry.propagate import inject
except ImportError:  # local unit-test environments may omit optional telemetry packages
    def inject(headers):
        return headers

from contracts.tool_routing import ToolRoutingRequest, ToolRoutingResponse


class ToolRoutingClient:
    def __init__(self, base_url: str, *, timeout_seconds: float = 5.0,
                 api_token: str | None = None):
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self.api_token = api_token

    async def route(self, request: ToolRoutingRequest) -> ToolRoutingResponse:
        async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
            headers = {}
            inject(headers)
            if self.api_token:
                headers["Authorization"] = f"Bearer {self.api_token}"
            response = await client.post(
                f"{self.base_url}/v1/tool-routing",
                json=request.model_dump(mode="json"),
                headers=headers,
            )
            response.raise_for_status()
        result = ToolRoutingResponse.model_validate(response.json())
        result.ensure_allowed(request)
        return result
