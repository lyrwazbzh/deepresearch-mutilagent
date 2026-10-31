import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from contracts.tool_routing import (
    CapabilitySet,
    ToolCall,
    ToolName,
    ToolRoutingRequest,
    ToolRoutingResponse,
)


ROOT = Path(__file__).resolve().parents[3]


def test_wire_schema_and_python_enum_are_in_sync():
    payload = json.loads(
        (ROOT / "contracts" / "deepresearch_tools.v1.json").read_text(encoding="utf-8")
    )
    assert payload["schema_version"] == "1.0"
    assert {tool["name"] for tool in payload["tools"]} == {
        item.value for item in ToolName
    }


def test_capability_references_are_required_when_enabled():
    with pytest.raises(ValidationError, match="kb_name"):
        CapabilitySet(kb_enabled=True)
    with pytest.raises(ValidationError, match="database_profile_id"):
        CapabilitySet(database_enabled=True)


def test_response_must_stay_inside_request_allow_list():
    request = ToolRoutingRequest(
        run_id="run-1",
        session_id="session-1",
        stage="research",
        query="检索动力电池趋势",
        allowed_tools=[ToolName.WEB_SEARCH],
    )
    response = ToolRoutingResponse(
        decision_id="decision-1",
        calls=[
            ToolCall(call_id="call-1", name=ToolName.LOCAL_RAG, arguments={})
        ],
        confidence=0.9,
        model_version="test",
    )

    with pytest.raises(ValueError, match="disallowed"):
        response.ensure_allowed(request)
