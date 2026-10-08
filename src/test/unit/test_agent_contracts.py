from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest
from pydantic import BaseModel, ConfigDict, ValidationError

from src.agent.subagents.implementations import ChiefArchitect, LeadWriter, PromptRenderer, RouteNode
from src.agent.tools.skill_management import (
    SkillDefinition,
    SkillLoader,
    SkillMetadata,
    SkillPolicyError,
    SkillRegistry,
    SkillRuntime,
    SkillUnavailableError,
)
from src.agent.schema.capability import CapabilitySnapshot, FrozenSkill
from src.agent.schema.model_config import FrozenModelBinding, ResolvedModelRevision
from src.agent.schema.task import TaskBudget
from src.agent.schema.runtime import ContextPacket, ExecutionContext
from src.agent.schema.state import ResearchState, RouteDecision
from src.agent.llm.runtime import FakeModelRuntime
from src.agent.backends.persistence.models import utcnow
from src.agent.runtime.agent_loop import AgentLoop
from src.agent.runtime.context import ContextBuilder
from src.agent.runtime.tool_runtime import (
    ToolDefinition,
    ToolExecutionContext,
    ToolPolicy,
    ToolPolicyRegistry,
    ToolRegistry,
    ToolResult,
)


class Token:
    def is_cancelled(self) -> bool:
        return False


class Args(BaseModel):
    model_config = ConfigDict(extra="forbid")
    value: str = "ok"


class MarkerAdapter:
    def __init__(self, marker: str) -> None:
        self.marker = marker

    def execute(
        self, context: ToolExecutionContext, normalized_arguments: dict[str, object]
    ) -> ToolResult:
        del context, normalized_arguments
        return ToolResult(status="success", data={"marker": self.marker})


def binding(key: str = "reasoning") -> FrozenModelBinding:
    return FrozenModelBinding(
        binding_key=key,  # type: ignore[arg-type]
        purpose="chat",
        primary=ResolvedModelRevision(model_config_id="model", version=1),
    )


def state() -> ResearchState:
    return ResearchState(
        tenant_id="tenant",
        user_id="user",
        thread_id="thread",
        task_id="task",
        query="new completed query",
        route={"mode": "deep"},
        plan={
            "hypotheses": [
                {
                    "hypothesis_id": "old",
                    "claim": "old claim",
                    "evidence_needed": ["source"],
                    "refuted_if": ["counterexample"],
                    "priority": 1,
                    "queries": [
                        {"query_id": "old_q", "channel": "web", "text": "old query"}
                    ],
                }
            ]
        },
    )


def execution(snapshot: CapabilitySnapshot | None = None) -> ExecutionContext:
    return ExecutionContext(
        tenant_id="tenant",
        user_id="user",
        task_id="task",
        thread_id="thread",
        lease_token="lease",
        cancellation_token=Token(),
        deadline=utcnow() + timedelta(minutes=1),
        capability_snapshot=snapshot.model_dump(mode="json") if snapshot else None,
    )


def test_route_schema_and_node_keep_the_control_boundary() -> None:
    with pytest.raises(ValidationError):
        RouteDecision.model_validate({"mode": "focused"})
    with pytest.raises(ValidationError):
        RouteDecision.model_validate({"mode": "direct", "target_agent": "deep_scout"})
    with pytest.raises(ValidationError):
        RouteDecision.model_validate({"mode": "complex"})

    runtime = FakeModelRuntime({"routing": [{"mode": "focused", "target_agent": "deep_scout"}]})
    packet = ContextPacket(
        role="route",
        task_id="task",
        query="find one paper",
        deadline=utcnow() + timedelta(minutes=1),
        payload={"available_execution_domains": ["deep_scout"]},
    )
    result = RouteNode().run(packet, runtime.build_chat_model("tenant", binding("routing")))
    assert result.patch.values["route"] == {
        "mode": "focused",
        "target_agent": "deep_scout",
    }
    assert result.patch.values["plan"] is None
    assert result.effects == []
    assert "Do not answer" in runtime.calls["routing"][0]
    assert result.output["agent_trace"]["prompt"]["revision"]


def test_chief_replan_has_a_distinct_context_prompt_and_validated_plan() -> None:
    current = state()
    packet = ContextBuilder().build(
        "chief_replan", current, execution(), TaskBudget()
    )
    assert packet.query == "new completed query"
    assert packet.payload["previous_plan"]["hypotheses"][0]["hypothesis_id"] == "old"
    assert "evidence_pool" not in packet.payload

    replacement = {
        "hypotheses": [
            {
                "hypothesis_id": "new",
                "claim": "new claim",
                "evidence_needed": ["primary source"],
                "refuted_if": ["contradiction"],
                "priority": 1,
                "queries": [
                    {"query_id": "new_q", "channel": "rag", "text": "new source"}
                ],
            }
        ],
        "deepen_history": [],
    }
    runtime = FakeModelRuntime({"reasoning": [replacement]})
    result = ChiefArchitect().replan(
        packet, runtime.build_chat_model("tenant", binding())
    )
    assert result.patch.values["plan"]["hypotheses"][0]["hypothesis_id"] == "new"
    assert "analysis_requests" not in result.patch.values
    assert "previous plan is context" in runtime.calls["reasoning"][0]


def test_prompt_identity_is_stable_and_prompts_state_untrusted_content_boundaries() -> None:
    renderer = PromptRenderer()
    first = renderer.identity("deep_scout.jinja2")
    second = renderer.identity("deep_scout.jinja2")
    assert first == second
    assert len(first.revision) == 64
    prompt_root = Path(__file__).resolve().parents[2] / "agent" / "prompts"
    assert "untrusted content" in (prompt_root / "deep_scout.jinja2").read_text("utf-8")
    assert "untrusted source material" in (prompt_root / "lead_writer.jinja2").read_text(
        "utf-8"
    )


def test_skill_runtime_progressive_disclosure_whitelist_and_script_boundary(tmp_path) -> None:
    root = tmp_path / "skills"
    skill_dir = root / "visualization"
    script_dir = skill_dir / "scripts"
    script_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: visualization\ndescription: Make a chart.\nrequired_tools:\n"
        "  - generate_chart\n---\n# Method\nUse the chart tool.\n",
        encoding="utf-8",
    )
    marker = tmp_path / "must-not-exist"
    (script_dir / "guide.py").write_text(
        f"from pathlib import Path\nPath({str(marker)!r}).write_text('executed')\n",
        encoding="utf-8",
    )
    registry = SkillRegistry(SkillLoader(root).load_all())
    runtime = SkillRuntime(registry)
    frozen = registry.snapshot({"visualization"})
    tool_registry = ToolRegistry(
        [
            ToolDefinition(
                name="generate_chart",
                args_schema=Args,
                adapter=MarkerAdapter("chart"),
            )
        ]
    )
    snapshot = CapabilitySnapshot(
        skills=frozen,
        tools=tool_registry.snapshot(),
        policy_revision="p1",
    )
    policy = ToolPolicy(frozenset({"generate_chart"}))

    assert [item.name for item in runtime.metadata("code_wizard", snapshot)] == [
        "visualization"
    ]
    with pytest.raises(SkillPolicyError):
        runtime.activate(
            agent_name="data_analyst",
            skill_name="visualization",
            snapshot=snapshot,
            policy=policy,
        )
    activated = runtime.activate(
        agent_name="code_wizard",
        skill_name="visualization",
        snapshot=snapshot,
        policy=policy,
    )
    assert "Use the chart tool" in activated.instructions
    resource = runtime.load_resource(
        agent_name="code_wizard",
        skill_name="visualization",
        resource_path="scripts/guide.py",
        snapshot=snapshot,
        policy=policy,
    )
    assert "write_text" in resource.content
    assert not marker.exists()

    denied_snapshot = snapshot.model_copy(update={"tools": []})
    with pytest.raises(SkillPolicyError):
        runtime.activate(
            agent_name="code_wizard",
            skill_name="visualization",
            snapshot=denied_snapshot,
            policy=policy,
        )


def test_agent_loop_activates_only_frozen_skill_then_uses_l2_in_next_decision() -> None:
    definition = SkillDefinition(
        metadata=SkillMetadata(
            name="rendered-web",
            description="Read a page",
            required_tools=[],
        ),
        content_hash="a" * 64,
        instructions="Use the minimum necessary source.",
        resources={},
    )
    skill_runtime = SkillRuntime(SkillRegistry([definition]))
    policies = {"deep_scout": ToolPolicy(frozenset())}
    policy_registry = ToolPolicyRegistry(policies)
    snapshot = CapabilitySnapshot(
        skills=[FrozenSkill(name="rendered-web", content_hash="a" * 64)],
        tools=[],
        policy_revision=ToolPolicyRegistry.revision(policies),
    )
    runtime = FakeModelRuntime(
        {
            "tool": [
                {"action": "activate_skill", "skill_name": "rendered-web"},
                {"action": "final", "answer": "done"},
            ]
        }
    )
    answer = AgentLoop(
        ToolRegistry(),
        max_steps=2,
        skill_runtime=skill_runtime,
        policy_registry=policy_registry,
    ).run(
        agent_name="deep_scout",
        model=runtime.build_chat_model("tenant", binding("tool")),
        packet=ContextPacket(
            role="deep_scout",
            task_id="task",
            query="query",
            deadline=utcnow() + timedelta(minutes=1),
        ),
        execution=execution(snapshot),
        policy=policies["deep_scout"],
        prompt="DeepScout contract",
    )
    assert answer == "done"
    assert "Use the minimum necessary source" in runtime.calls["tool"][1]


def test_frozen_tool_and_policy_revisions_survive_latest_registry_changes() -> None:
    v1 = ToolDefinition(
        name="search_web",
        args_schema=Args,
        adapter=MarkerAdapter("v1"),
        provider_ref="local:web:v1",
    )
    registry = ToolRegistry([v1])
    policies_v1 = {"deep_scout": ToolPolicy(frozenset({"search_web"}))}
    policy_registry = ToolPolicyRegistry(policies_v1)
    snapshot = CapabilitySnapshot(
        tools=registry.snapshot(),
        skills=[],
        policy_revision=ToolPolicyRegistry.revision(policies_v1),
    )
    registry.replace(
        ToolDefinition(
            name="search_web",
            args_schema=Args,
            adapter=MarkerAdapter("v2"),
            provider_ref="local:web:v2",
        )
    )
    policy_registry.register({"deep_scout": ToolPolicy(frozenset())})

    assert registry.resolve(snapshot.tools[0]).adapter is v1.adapter
    assert policy_registry.resolve(snapshot.policy_revision, "deep_scout").allows(
        "search_web"
    )
    missing = snapshot.model_copy(
        update={
            "skills": [FrozenSkill(name="rendered-web", content_hash="f" * 64)]
        }
    )
    with pytest.raises(SkillUnavailableError):
        SkillRuntime(SkillRegistry()).metadata("deep_scout", missing)


def test_writer_rejects_citation_outside_approved_evidence_before_graph_state_changes() -> None:
    candidate = {
        "report_version": 1,
        "title": "Report",
        "sections": [
            {
                "section_id": "s1",
                "heading": "Finding",
                "level": 1,
                "blocks": [
                    {
                        "type": "paragraph",
                        "segments": [
                            {
                                "type": "claim",
                                "claim_id": "c1",
                                "text": "Unsupported",
                                "citation_ids": ["cit1"],
                            }
                        ],
                    }
                ],
            }
        ],
        "citations": [{"citation_id": "cit1", "evidence_id": "ev_unknown"}],
    }
    runtime = FakeModelRuntime({"writing": [candidate]})
    packet = ContextPacket(
        role="lead_writer",
        task_id="task",
        query="query",
        deadline=utcnow() + timedelta(minutes=1),
        payload={"approved_evidence": [{"evidence_id": "ev_allowed"}]},
    )
    with pytest.raises(ValueError, match="outside approved context"):
        LeadWriter().write(packet, runtime.build_chat_model("tenant", binding("writing")))
