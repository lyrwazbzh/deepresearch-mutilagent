"""Deterministic source expansion used when the learned router is unavailable."""

from __future__ import annotations

import uuid

from contracts.tool_routing import ToolCall, ToolName, ToolRoutingRequest, ToolRoutingResponse


class DeterministicToolRouter:
    async def route(self, request: ToolRoutingRequest) -> ToolRoutingResponse:
        calls: list[ToolCall] = []
        capabilities = request.capabilities
        for tool in request.allowed_tools:
            arguments: dict[str, object] | None = None
            if tool == ToolName.WEB_SEARCH and capabilities.web_enabled:
                arguments = {"query": request.query, "count": 10}
            elif tool == ToolName.LOCAL_RAG and capabilities.kb_enabled:
                arguments = {"query": request.query, "kb_name": capabilities.kb_name, "top_k": 10}
            elif tool == ToolName.TEXT2SQL and capabilities.database_enabled:
                arguments = {
                    "question": request.query,
                    "intent": "stats",
                    "database_profile_id": capabilities.database_profile_id,
                }
            elif tool == ToolName.DATA_ANALYSIS:
                arguments = {"analysis_type": "auto", "goal": request.query}
            elif tool == ToolName.CHART_GENERATION:
                arguments = {"chart_type": "auto", "title": request.query[:120]}
            if arguments is not None:
                calls.append(ToolCall(
                    call_id=f"call_{uuid.uuid4().hex[:16]}",
                    name=tool,
                    arguments=arguments,
                ))
        if not calls:
            raise ValueError("no allowed tool is available for this request")
        return ToolRoutingResponse(
            decision_id=f"decision_{uuid.uuid4().hex}",
            calls=calls[:3],
            confidence=1.0,
            model_version="deterministic-fallback",
        )
