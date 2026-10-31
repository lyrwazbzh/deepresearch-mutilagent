"""Environment-backed tool router factory."""

from __future__ import annotations

import os
from functools import lru_cache

from .client import ToolRoutingClient
from .fallback import DeterministicToolRouter
from .service import ToolRoutingService


@lru_cache(maxsize=1)
def get_tool_router() -> ToolRoutingService:
    enabled = os.getenv("TOOL_ROUTER_ENABLED", "0") == "1"
    base_url = os.getenv("TOOL_ROUTER_BASE_URL", "").strip()
    adapter = None
    if enabled and base_url:
        adapter = ToolRoutingClient(
            base_url,
            timeout_seconds=float(os.getenv("TOOL_ROUTER_TIMEOUT_SECONDS", "5")),
            api_token=os.getenv("TOOL_ROUTER_API_TOKEN", "").strip() or None,
        )
    return ToolRoutingService(
        fallback=DeterministicToolRouter(),
        adapter=adapter,
        gray_percent=int(os.getenv("TOOL_ROUTER_GRAY_PERCENT", "0")),
        min_confidence=float(os.getenv("TOOL_ROUTER_MIN_CONFIDENCE", "0.65")),
    )
