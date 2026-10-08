from __future__ import annotations

from datetime import timedelta
from typing import Any

from pydantic import BaseModel, ConfigDict
from sqlalchemy import select

from src.agent.schema.capability import CapabilitySnapshot
from src.agent.schema.model_config import (
    FrozenModelBinding,
    ModelBindingSnapshot,
    ResolvedModelRevision,
)
from src.agent.schema.runtime import ContextPacket, ExecutionContext
from src.agent.evaluation.tool_flywheel import (
    FlywheelArtifactStore,
    FlywheelDatasetLoader,
    OfflineToolEvaluator,
    OfflineToolGate,
    ThreadSplitPolicy,
    ToolDatasetBuilder,
    ToolDecisionPrediction,
    ToolGateConfig,
)
from src.agent.llm.runtime import FakeModelRuntime
from src.agent.backends.persistence.models import DecisionTraceRow, ResearchTaskRow, ThreadRow, utcnow
from src.agent.runtime.agent_loop import AgentLoop
from src.agent.runtime.tool_runtime import (
    ToolDefinition,
    ToolExecutionContext,
    ToolPolicy,
    ToolPolicyRegistry,
    ToolRegistry,
    ToolResult,
)
from src.agent.runtime.tool_decisions import DecisionTraceService, capability_snapshot_ref
from src.agent.runtime.tool_decisions import (
    ArgumentAssertion,
    ToolDecisionGovernance,
    ToolDecisionReviewService,
    ToolLabelBuilder,
    TrainingPermissionPolicy,
)
from src.agent.backends.workspace import InMemoryObjectStore


class ToolArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    query: str


class RecordingAdapter:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def execute(
        self, context: ToolExecutionContext, normalized_arguments: dict[str, Any]
    ) -> ToolResult:
        self.calls.append(normalized_arguments)
        return ToolResult(
            status="success",
            summary=f"result for {normalized_arguments['query']}",
            data={"value": 1},
        )


class Token:
    def is_cancelled(self) -> bool:
        return False


def _binding(key: str) -> FrozenModelBinding:
    return FrozenModelBinding(
        binding_key=key,  # type: ignore[arg-type]
        purpose="chat",
        primary=ResolvedModelRevision(model_config_id="model_test", version=1),
    )


def _setup(session_factory):
    adapters: dict[str, RecordingAdapter] = {}
    definitions: list[ToolDefinition] = []
    tools = {
        "deep_scout": ("tool", "search_web"),
        "data_analyst": ("data", "inspect_schema"),
        "code_wizard": ("code", "sandbox_execute"),
    }
    for _agent, (_binding_key, tool_name) in tools.items():
        adapter = RecordingAdapter()
        adapters[tool_name] = adapter
        definitions.append(
            ToolDefinition(
                name=tool_name,
                args_schema=ToolArgs,
                adapter=adapter,
                provider_ref=f"fake:{tool_name}:v1",
            )
        )
    registry = ToolRegistry(definitions)
    policies = {
        agent: ToolPolicy(frozenset({tool_name}))
        for agent, (_key, tool_name) in tools.items()
    }
    policy_registry = ToolPolicyRegistry(policies)
    capability = CapabilitySnapshot(
        tools=registry.snapshot(),
        policy_revision=policy_registry.revision(policies),
    )
    snapshot = ModelBindingSnapshot(
        bindings={
            "routing": _binding("routing"),
            "reasoning": _binding("reasoning"),
            "tool": _binding("tool"),
            "data": _binding("data"),
            "code": _binding("code"),
            "writing": _binding("writing"),
        }
    )
    with session_factory() as session, session.begin():
        session.add(
            ThreadRow(
                thread_id="thread_phase13",
                tenant_id="tenant_test",
                user_id="user_test",
            )
        )
        session.add(
            ResearchTaskRow(
                task_id="task_phase13",
                tenant_id="tenant_test",
                user_id="user_test",
                thread_id="thread_phase13",
                status="running",
                latest_state={},
                model_binding_snapshot=snapshot.model_dump(mode="json"),
                capability_snapshot=capability.model_dump(mode="json"),
                create_idempotency_key="phase13",
                create_request_hash="1" * 64,
                task_budget={"budget": {}, "usage": {}},
            )
        )
    return registry, policies, policy_registry, capability, snapshot, adapters, tools


def _execute_agent(
    *,
    session_factory,
    registry,
    policies,
    policy_registry,
    capability,
    snapshot,
    agent_name: str,
    node_run_id: str,
    trace_service,
) -> FakeModelRuntime:
    binding_key = {"deep_scout": "tool", "data_analyst": "data", "code_wizard": "code"}[
        agent_name
    ]
    tool_name = next(iter(policies[agent_name].allowed_tools))
    runtime = FakeModelRuntime(
        {
            binding_key: [
                {
                    "action": "tool",
                    "tool_name": tool_name,
                    "arguments": {"query": f"{agent_name} current query"},
                },
                {"action": "final", "answer": "done"},
            ]
        }
    )
    packet = ContextPacket(
        role=agent_name,  # type: ignore[arg-type]
        task_id="task_phase13",
        query="research user@example.com with api_key=secret-value",
        deadline=utcnow() + timedelta(minutes=2),
        payload={
            "runtime": {"node_run_id": node_run_id},
            "branch": "focused",
        },
    )
    execution = ExecutionContext(
        tenant_id="tenant_test",
        user_id="user_test",
        task_id="task_phase13",
        thread_id="thread_phase13",
        lease_token="lease",
        cancellation_token=Token(),
        deadline=utcnow() + timedelta(minutes=2),
        capability_snapshot=capability.model_dump(mode="json"),
    )
    answer = AgentLoop(
        registry,
        max_steps=2,
        policy_registry=policy_registry,
        decision_trace_service=trace_service,
    ).run(
        agent_name=agent_name,
        model=runtime.build_chat_model(
            "tenant_test", snapshot.get(binding_key)  # type: ignore[arg-type]
        ),
        packet=packet,
        execution=execution,
        policy=policies[agent_name],
        prompt="bounded react",
    )
    assert answer == "done"
    return runtime


def test_three_execution_agents_capture_tool_and_no_tool_idempotently(
    session_factory,
) -> None:
    (
        registry,
        policies,
        policy_registry,
        capability,
        snapshot,
        adapters,
        tools,
    ) = _setup(session_factory)
    trace_service = DecisionTraceService(session_factory)
    runtimes: dict[str, FakeModelRuntime] = {}
    for agent_name in tools:
        runtimes[agent_name] = _execute_agent(
            session_factory=session_factory,
            registry=registry,
            policies=policies,
            policy_registry=policy_registry,
            capability=capability,
            snapshot=snapshot,
            agent_name=agent_name,
            node_run_id=f"node_{agent_name}",
            trace_service=trace_service,
        )
    with session_factory() as session:
        rows = list(
            session.scalars(
                select(DecisionTraceRow).order_by(
                    DecisionTraceRow.agent_name, DecisionTraceRow.decision_index
                )
            )
        )
    assert len(rows) == 6
    assert {row.agent_name for row in rows} == set(tools)
    assert {row.decision["selected_tool"] for row in rows} == {
        "search_web",
        "inspect_schema",
        "sandbox_execute",
        "NO_TOOL",
    }
    for row in rows:
        assert row.context["toolset_snapshot_ref"] == capability_snapshot_ref(capability)
        assert len(row.context["available_tools"]) == 1
        if row.decision_index == 2:
            assert row.context["prior_tool_results"][0]["status"] == "success"
            assert row.context["prior_tool_results"][0]["summary"].startswith("result for")
    for agent_name, runtime in runtimes.items():
        binding_key = tools[agent_name][0]
        second_prompt = runtime.calls[binding_key][1]
        row = next(
            item for item in rows if item.agent_name == agent_name and item.decision_index == 2
        )
        assert str(row.context["prior_tool_results"]) in second_prompt

    # Same semantic NodeRun/decision positions reuse the original candidates.
    for agent_name in tools:
        _execute_agent(
            session_factory=session_factory,
            registry=registry,
            policies=policies,
            policy_registry=policy_registry,
            capability=capability,
            snapshot=snapshot,
            agent_name=agent_name,
            node_run_id=f"node_{agent_name}",
            trace_service=trace_service,
        )
    with session_factory() as session:
        assert len(list(session.scalars(select(DecisionTraceRow)))) == 6
    assert all(adapter.calls for adapter in adapters.values())


def test_capture_failure_and_out_of_scope_agent_do_not_break_execution(
    session_factory,
) -> None:
    registry, policies, policy_registry, capability, snapshot, adapters, _ = _setup(
        session_factory
    )

    class BrokenTraceService:
        def capture(self, **_kwargs):
            raise ConnectionError("side writer unavailable")

        def record_execution(self, *_args, **_kwargs):
            raise AssertionError("capture produced no decision id")

    _execute_agent(
        session_factory=session_factory,
        registry=registry,
        policies=policies,
        policy_registry=policy_registry,
        capability=capability,
        snapshot=snapshot,
        agent_name="deep_scout",
        node_run_id="node_capture_failure",
        trace_service=BrokenTraceService(),
    )
    assert adapters["search_web"].calls

    runtime = FakeModelRuntime({"reasoning": [{"action": "final", "answer": "done"}]})
    packet = ContextPacket(
        role="deep_scout",
        task_id="task_phase13",
        query="q",
        deadline=utcnow() + timedelta(minutes=1),
        payload={"runtime": {"node_run_id": "node_chief"}},
    )
    execution = ExecutionContext(
        tenant_id="tenant_test",
        user_id="user_test",
        task_id="task_phase13",
        thread_id="thread_phase13",
        lease_token="lease",
        cancellation_token=Token(),
        deadline=utcnow() + timedelta(minutes=1),
        capability_snapshot=capability.model_dump(mode="json"),
    )
    answer = AgentLoop(
        registry,
        max_steps=1,
        policy_registry=policy_registry,
        decision_trace_service=DecisionTraceService(session_factory),
    ).run(
        agent_name="chief_architect",
        model=runtime.build_chat_model("tenant_test", _binding("reasoning")),
        packet=packet,
        execution=execution,
        policy=ToolPolicy(frozenset()),
        prompt="not an execution agent",
    )
    assert answer == "done"
    with session_factory() as session:
        assert not list(session.scalars(select(DecisionTraceRow)))


def test_real_trace_governance_dataset_eval_and_gate_chain(session_factory) -> None:
    registry, policies, policy_registry, capability, snapshot, _adapters, tools = _setup(
        session_factory
    )
    traces = DecisionTraceService(session_factory)
    for agent_name in tools:
        _execute_agent(
            session_factory=session_factory,
            registry=registry,
            policies=policies,
            policy_registry=policy_registry,
            capability=capability,
            snapshot=snapshot,
            agent_name=agent_name,
            node_run_id=f"node_dataset_{agent_name}",
            trace_service=traces,
        )
    rows = traces.list_candidates(tenant_id="tenant_test", user_id="user_test")
    governance = ToolDecisionGovernance(
        session_factory,
        registry,
        TrainingPermissionPolicy(
            allowed_tenants=frozenset({"tenant_test"}),
            allowed_users=frozenset({("tenant_test", "user_test")}),
        ),
    )
    reviews = ToolDecisionReviewService(session_factory)
    label_builder = ToolLabelBuilder()
    for row in rows:
        governance_result, governed = governance.evaluate(row)
        assert governance_result.eligible
        assert governed is not None
        selected = governed.decision.selected_tool
        assertions = (
            [ArgumentAssertion(kind="required", path="query")]
            if selected != "NO_TOOL"
            else []
        )
        label = label_builder.build(
            governed,
            acceptable_tools=[selected],
            argument_assertions=assertions,
            label_source="deterministic_rule",
            confidence="high",
            difficulty="trivial",
        )
        reviews.submit(
            decision_id=row.decision_id,
            reviewer_ref="system:phase13-test",
            disposition="accepted",
            label=label,
        )

    object_store = InMemoryObjectStore()
    artifact_store = FlywheelArtifactStore(object_store)
    split_policy = next(
        ThreadSplitPolicy(train_percent=98, val_percent=1, seed=f"seed-{index}")
        for index in range(100)
        if ThreadSplitPolicy(
            train_percent=98, val_percent=1, seed=f"seed-{index}"
        ).split("thread_phase13")
        == "train"
    )
    builder = ToolDatasetBuilder(
        artifact_store,
        governance,
        reviews,
        split_policy=split_policy,
    )
    snapshot_result = builder.build(
        dataset_id="tool-decisions",
        version="v1",
        traces=rows,
        git_commit="phase13-test-revision",
        created_at=utcnow(),
    )
    assert snapshot_result.manifest["sample_count"] == 6
    assert snapshot_result.manifest["split_counts"] == {
        "train": 6,
        "val": 0,
        "test": 0,
    }
    assert snapshot_result.manifest["agent_distribution"] == {
        "code_wizard": 2,
        "data_analyst": 2,
        "deep_scout": 2,
    }
    serialized = b"".join(
        object_store.get(ref)
        for ref in object_store.list_prefix(snapshot_result.dataset_ref)
    ).decode("utf-8")
    assert "tenant_test" not in serialized
    assert "user_test" not in serialized
    assert "task_phase13" not in serialized
    assert "user@example.com" not in serialized
    assert "secret-value" not in serialized
    try:
        builder.build(
            dataset_id="tool-decisions",
            version="v1",
            traces=rows,
            git_commit="phase13-test-revision",
        )
    except FileExistsError:
        pass
    else:
        raise AssertionError("Published snapshot was overwritten")

    loader = FlywheelDatasetLoader(artifact_store)
    samples = loader.load_split(snapshot_result.dataset_ref, "train")

    def perfect(context: dict[str, Any]) -> ToolDecisionPrediction:
        tool_observations = [
            item for item in context["messages"] if item["role"] == "tool"
        ]
        if tool_observations:
            return ToolDecisionPrediction(
                selected_tool="NO_TOOL", execution_status="success"
            )
        tool_name = context["tools"][0]["function"]["name"]
        agent = context["messages"][0]["content"].split()[3]
        return ToolDecisionPrediction(
            selected_tool=tool_name,
            arguments={"query": f"{agent} current query"},
            execution_status="success",
        )

    evaluator = OfflineToolEvaluator()
    candidate = evaluator.evaluate(samples, perfect)
    baseline = evaluator.evaluate(
        samples,
        lambda _context: ToolDecisionPrediction(
            selected_tool="NO_TOOL", execution_status="error"
        ),
    )
    assert candidate.overall["tool_selection_accuracy"] == 1.0
    assert candidate.overall["argument_assertion_pass_rate"] == 1.0
    assert candidate.overall["tool_decision_one_shot_success"] == 1.0
    assert candidate.overall["execution_success"] == 1.0
    assert set(candidate.by_agent) == set(tools)
    assert set(candidate.by_difficulty) == {"trivial"}
    gate = OfflineToolGate(artifact_store).compare(
        dataset_ref=snapshot_result.dataset_ref,
        baseline=baseline,
        candidate=candidate,
        baseline_config={"binding": "baseline"},
        candidate_config={"binding": "candidate"},
        code_revision="phase13-test-revision",
        gate_config=ToolGateConfig(
            version="gate-v1", selection_delta=0.1, one_shot_delta=0.1
        ),
        created_at=utcnow(),
    )
    assert gate.passed
    assert object_store.exists(f"flywheel-evaluations/{gate.evaluation_id}.json")


def test_governance_rejects_schema_mismatch_permission_and_contamination(
    session_factory,
) -> None:
    registry, policies, policy_registry, capability, snapshot, _adapters, _tools = _setup(
        session_factory
    )
    traces = DecisionTraceService(session_factory)
    _execute_agent(
        session_factory=session_factory,
        registry=registry,
        policies=policies,
        policy_registry=policy_registry,
        capability=capability,
        snapshot=snapshot,
        agent_name="deep_scout",
        node_run_id="node_invalid",
        trace_service=traces,
    )
    rows = traces.list_candidates(tenant_id="tenant_test")
    denied = ToolDecisionGovernance(
        session_factory, registry, TrainingPermissionPolicy()
    )
    denied_result, _ = denied.evaluate(rows[0])
    assert "training_permission_denied" in denied_result.reasons

    with session_factory() as session, session.begin():
        row = session.get(DecisionTraceRow, rows[0].decision_id)
        assert row is not None
        context = dict(row.context)
        available = [dict(item) for item in context["available_tools"]]
        available[0]["schema_hash"] = "f" * 64
        context["available_tools"] = available
        row.context = context
    allowed = ToolDecisionGovernance(
        session_factory,
        registry,
        TrainingPermissionPolicy(
            allowed_tenants=frozenset({"tenant_test"}),
            allowed_users=frozenset({("tenant_test", "user_test")}),
        ),
    )
    with session_factory() as session:
        changed = session.get(DecisionTraceRow, rows[0].decision_id)
        assert changed is not None
        result, governed = allowed.evaluate(changed)
    assert governed is None
    assert "tool_schema_mismatch" in result.reasons
