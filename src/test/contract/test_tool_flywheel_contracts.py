from __future__ import annotations

from datetime import datetime, timezone

import pytest

from src.agent.evaluation.tool_flywheel import ToolDecisionPrediction
from src.agent.evaluation.model_rollout import ToolShadowOrchestrator
from src.agent.runtime.tool_decisions import (
    ArgumentAssertion,
    AvailableTool,
    GovernedToolDecision,
    ToolDecisionContext,
    ToolDecisionLabel,
    ToolDecisionValue,
    ToolLabelBuilder,
    ToolSampleBuilder,
    argument_assertions_pass,
)


def governed(selected_tool: str = "search_web") -> GovernedToolDecision:
    schema = {
        "type": "object",
        "properties": {
            "query": {"type": "string"},
            "limit": {"type": "integer", "minimum": 1, "maximum": 10},
        },
        "required": ["query", "limit"],
        "additionalProperties": False,
    }
    return GovernedToolDecision(
        decision_id="decision_contract",
        tenant_id="tenant_redacted_before_sample",
        thread_id="thread_internal_split_only",
        agent_name="deep_scout",
        created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        context=ToolDecisionContext(
            current_intent="find current sources",
            toolset_snapshot_ref="a" * 64,
            available_tools=[AvailableTool(name="search_web", schema_hash="b" * 64)],
            prior_tool_results=[],
        ),
        decision=ToolDecisionValue(
            selected_tool=selected_tool,
            arguments=(
                {"query": "market outlook", "limit": 5}
                if selected_tool != "NO_TOOL"
                else None
            ),
        ),
        tool_schemas={"search_web": schema},
        execution=None,
        outcome={},
    )


def test_label_contract_supports_multiple_tools_no_tool_and_assertions() -> None:
    item = governed()
    assertions = [
        ArgumentAssertion(kind="required", path="query"),
        ArgumentAssertion(kind="enum", path="limit", values=[3, 5]),
        ArgumentAssertion(kind="range", path="limit", minimum=1, maximum=10),
        ArgumentAssertion(kind="predicate", path="query", predicate="non_empty"),
    ]
    label = ToolLabelBuilder().build(
        item,
        acceptable_tools=["search_web", "NO_TOOL"],
        argument_assertions=assertions,
        label_source="deterministic_rule",
        confidence="high",
        difficulty="confusable",
    )
    assert label.acceptable_tools == ["search_web", "NO_TOOL"]
    assert argument_assertions_pass(item.decision.arguments or {}, assertions)
    assert not argument_assertions_pass(
        {"query": "", "limit": 11}, assertions
    )
    with pytest.raises(ValueError, match="explicit review"):
        ToolLabelBuilder().build(
            item,
            acceptable_tools=["search_web"],
            label_source="validated_outcome",
            confidence="medium",
            difficulty="trivial",
        )


def test_sample_is_native_tool_call_or_no_tool_and_curated_eval_isolated() -> None:
    tool_item = governed()
    label = ToolDecisionLabel(
        acceptable_tools=["search_web"],
        argument_assertions=[ArgumentAssertion(kind="required", path="query")],
        label_source="human_gold",
        confidence="high",
        difficulty="trivial",
    )
    sample = ToolSampleBuilder().build(tool_item, label).payload
    call = sample["output"]["tool_calls"][0]
    assert call["type"] == "function"
    assert call["function"]["name"] == "search_web"
    assert '"query":"market outlook"' in call["function"]["arguments"]
    assert "tenant_redacted_before_sample" not in str(sample)
    assert "thread_internal_split_only" not in str(sample)

    no_tool = governed("NO_TOOL")
    no_tool_label = label.model_copy(update={"acceptable_tools": ["NO_TOOL"]})
    no_tool_sample = ToolSampleBuilder().build(no_tool, no_tool_label).payload
    assert no_tool_sample["output"] == {
        "role": "assistant",
        "content": "NO_TOOL",
        "tool_calls": [],
    }
    with pytest.raises(ValueError, match="Evaluation"):
        ToolSampleBuilder().build(
            tool_item,
            label.model_copy(update={"label_source": "curated_eval"}),
        )


def test_shadow_uses_same_context_and_never_accepts_executed_candidate() -> None:
    sample = ToolSampleBuilder().build(
        governed(),
        ToolDecisionLabel(
            acceptable_tools=["search_web"],
            argument_assertions=[ArgumentAssertion(kind="required", path="query")],
            label_source="deterministic_rule",
            confidence="high",
            difficulty="trivial",
        ),
    ).payload
    seen: list[dict] = []

    def candidate(context):
        seen.append(context)
        return ToolDecisionPrediction(
            selected_tool="search_web",
            arguments={"query": "market outlook", "limit": 5},
        )

    result = ToolShadowOrchestrator().compare(
        context=sample["input"],
        sample_metadata=sample,
        production=ToolDecisionPrediction(
            selected_tool="search_web",
            arguments={"query": "market outlook", "limit": 5},
            execution_status="success",
        ),
        label=ToolDecisionLabel.model_validate(sample["label"]),
        candidate_model=candidate,
    )
    assert seen == [sample["input"]]
    assert not result.disagreement
    assert result.candidate_one_shot_success

    with pytest.raises(ValueError, match="never execute"):
        ToolShadowOrchestrator().compare(
            context=sample["input"],
            sample_metadata=sample,
            production=ToolDecisionPrediction(selected_tool="NO_TOOL"),
            label=ToolDecisionLabel.model_validate(sample["label"]),
            candidate_model=lambda _context: ToolDecisionPrediction(
                selected_tool="search_web",
                arguments={"query": "x", "limit": 1},
                execution_status="success",
            ),
        )
