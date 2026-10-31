"""Stable-hash gray routing with validated fallback."""

from __future__ import annotations

import hashlib
import logging
from typing import Protocol

from contracts.tool_routing import ToolRoutingRequest, ToolRoutingResponse
from observability.metrics import application_metrics


logger = logging.getLogger(__name__)


class Router(Protocol):
    async def route(self, request: ToolRoutingRequest) -> ToolRoutingResponse: ...


class ToolRoutingService:
    def __init__(self, *, fallback: Router, adapter: Router | None = None,
                 gray_percent: int = 0, min_confidence: float = 0.65):
        if not 0 <= gray_percent <= 100:
            raise ValueError("gray_percent must be between 0 and 100")
        self.fallback = fallback
        self.adapter = adapter
        self.gray_percent = gray_percent
        self.min_confidence = min_confidence

    def adapter_selected(self, request: ToolRoutingRequest) -> bool:
        if self.adapter is None or self.gray_percent == 0:
            return False
        key = f"{request.session_id}:{request.schema_version}".encode("utf-8")
        bucket = int.from_bytes(hashlib.sha256(key).digest()[:4], "big") % 100
        return bucket < self.gray_percent

    async def route(self, request: ToolRoutingRequest) -> ToolRoutingResponse:
        if not self.adapter_selected(request):
            response = await self.fallback.route(request)
            application_metrics.tool_router_decisions.labels("fallback", "success").inc()
            return response
        try:
            response = await self.adapter.route(request)
            response.ensure_allowed(request)
            if response.confidence < self.min_confidence:
                raise ValueError("adapter confidence below threshold")
            application_metrics.tool_router_decisions.labels("adapter", "success").inc()
            return response
        except Exception as exc:
            logger.warning("tool router fallback: %s", type(exc).__name__)
            application_metrics.tool_router_decisions.labels("adapter", "fallback").inc()
            response = await self.fallback.route(request)
            application_metrics.tool_router_decisions.labels("fallback", "success").inc()
            return response
