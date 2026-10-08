from __future__ import annotations

import json
import tempfile
import uuid
from datetime import timedelta
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict
from sqlalchemy import func, select

from src.agent.config import Settings
from src.agent.schema.capability import CapabilitySnapshot
from src.agent.schema.model_config import (
    FrozenModelBinding,
    ModelBindingSnapshot,
    ResolvedModelRevision,
)
from src.agent.schema.runtime import ContextPacket, ExecutionContext
from src.agent.evaluation.tool_flywheel import (
    FilesystemArtifactObjectStore,
    FlywheelArtifactStore,
    FlywheelDatasetLoader,
    OfflineToolEvaluator,
    OfflineToolGate,
    ThreadSplitPolicy,
    ToolDatasetBuilder,
    ToolDecisionPrediction,
    ToolGateConfig,
)
from src.agent.llm.runtime import ModelRuntime, ProviderChatResponse
from src.agent.backends.persistence.database import create_session_factory
from src.agent.backends.persistence.models import (
    DecisionTraceRow,
    ModelConfigRow,
    ResearchTaskRow,
    ThreadRow,
    ToolDecisionReviewRow,
    utcnow,
)
from src.agent.runtime.agent_loop import AgentLoop
from src.agent.runtime.tool_runtime import (
    ToolDefinition,
    ToolExecutionContext,
    ToolPolicy,
    ToolPolicyRegistry,
    ToolRegistry,
    ToolResult,
)
from src.agent.runtime.tool_decisions import (
    NO_TOOL,
    ArgumentAssertion,
    DecisionTraceService,
    ToolDecisionGovernance,
    ToolDecisionReviewService,
    ToolLabelBuilder,
    TrainingPermissionPolicy,
)
from src.agent.evaluation.training import (
    DryRunToolTrainingBackend,
    ToolTrainingConfig,
    ToolTrainingPipeline,
)


class SmokeToolArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    query: str


class SmokeToolAdapter:
    """Deterministic external-Tool boundary; ToolRuntime itself remains production code."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def execute(
        self, context: ToolExecutionContext, normalized_arguments: dict[str, Any]
    ) -> ToolResult:
        self.calls.append(normalized_arguments)
        return ToolResult(
            status="success",
            summary=f"bounded result for {normalized_arguments['query']}",
            data={"row_count": 1, "result": "deterministic-boundary-value"},
        )


class ScriptedProviderBoundary:
    """Provider-boundary fake exercised through the real versioned ModelRuntime."""

    def __init__(self, tool_by_agent: dict[str, str]) -> None:
        self.tool_by_agent = tool_by_agent
        self.chat_calls = 0

    def chat(self, _config: Any, prompt: str) -> ProviderChatResponse:
        self.chat_calls += 1
        agent = next(
            (
                candidate
                for candidate in self.tool_by_agent
                if f'"role":"{candidate}"' in prompt
            ),
            None,
        )
        if agent is None:
            raise AssertionError("Phase 13 smoke provider could not recover execution agent")
        if "Observations:\n[]" in prompt:
            content = json.dumps(
                {
                    "action": "tool",
                    "tool_name": self.tool_by_agent[agent],
                    "arguments": {"query": f"{agent} current query"},
                    "logical_call_slot": "primary",
                }
            )
        else:
            content = json.dumps({"action": "final", "answer": "done"})
        return ProviderChatResponse(content=content, input_tokens=12, output_tokens=8)

    def embed(self, _config: Any, texts: list[str]) -> list[list[float]]:
        return [[0.0] for _ in texts]

    def rerank(
        self, _config: Any, _query: str, candidates: list[str]
    ) -> list[float]:
        return [0.0 for _ in candidates]


class NeverCancelled:
    def is_cancelled(self) -> bool:
        return False


def _binding(key: str, model_config_id: str) -> FrozenModelBinding:
    return FrozenModelBinding(
        binding_key=key,  # type: ignore[arg-type]
        purpose="chat",
        primary=ResolvedModelRevision(model_config_id=model_config_id, version=1),
    )


def _perfect_prediction(context: dict[str, Any]) -> ToolDecisionPrediction:
    prior_results = [item for item in context["messages"] if item["role"] == "tool"]
    if prior_results:
        return ToolDecisionPrediction(selected_tool=NO_TOOL, execution_status="success")
    tool_name = context["tools"][0]["function"]["name"]
    agent = context["messages"][0]["content"].split()[3]
    return ToolDecisionPrediction(
        selected_tool=tool_name,
        arguments={"query": f"{agent} current query"},
        execution_status="success",
    )


def main() -> None:
    settings = Settings.from_env()
    if not settings.database_url.startswith("postgresql"):
        raise RuntimeError("Phase 13 real smoke requires the configured PostgreSQL database")

    suffix = uuid.uuid4().hex[:12]
    tenant_id = f"tenant_p13_{suffix}"
    user_id = f"user_p13_{suffix}"
    thread_id = f"thread_p13_{suffix}"
    task_id = f"task_p13_{suffix}"
    model_config_id = f"model_p13_{suffix}"
    sessions = create_session_factory(settings.database_url)

    agent_tools = {
        "deep_scout": ("tool", "phase13_search_web"),
        "data_analyst": ("data", "phase13_inspect_schema"),
        "code_wizard": ("code", "phase13_sandbox_execute"),
    }
    adapters: dict[str, SmokeToolAdapter] = {}
    registry = ToolRegistry()
    for _agent, (_binding_key, tool_name) in agent_tools.items():
        adapter = SmokeToolAdapter()
        adapters[tool_name] = adapter
        registry.register(
            ToolDefinition(
                name=tool_name,
                args_schema=SmokeToolArgs,
                adapter=adapter,
                provider_ref=f"phase13-smoke:{tool_name}:v1",
            )
        )
    policies = {
        agent: ToolPolicy(frozenset({tool_name}))
        for agent, (_binding_key, tool_name) in agent_tools.items()
    }
    policy_registry = ToolPolicyRegistry(policies)
    capability = CapabilitySnapshot(
        tools=registry.snapshot(), policy_revision=policy_registry.revision(policies)
    )
    model_snapshot = ModelBindingSnapshot(
        bindings={
            "routing": _binding("routing", model_config_id),
            "reasoning": _binding("reasoning", model_config_id),
            "tool": _binding("tool", model_config_id),
            "data": _binding("data", model_config_id),
            "code": _binding("code", model_config_id),
            "writing": _binding("writing", model_config_id),
        }
    )

    with sessions() as session, session.begin():
        session.add(
            ModelConfigRow(
                model_config_id=model_config_id,
                version=1,
                tenant_id=tenant_id,
                user_id=user_id,
                provider="phase13-smoke-boundary",
                model="deterministic-nonbillable",
                purpose="chat",
            )
        )
        session.add(
            ThreadRow(
                thread_id=thread_id,
                tenant_id=tenant_id,
                user_id=user_id,
            )
        )
        # These rows intentionally have no ORM relationship, so make the FK
        # dependency explicit for PostgreSQL rather than relying on flush order.
        session.flush()
        session.add(
            ResearchTaskRow(
                task_id=task_id,
                tenant_id=tenant_id,
                user_id=user_id,
                thread_id=thread_id,
                status="running",
                latest_state={},
                model_binding_snapshot=model_snapshot.model_dump(mode="json"),
                capability_snapshot=capability.model_dump(mode="json"),
                create_idempotency_key=f"phase13-real-{suffix}",
                create_request_hash="d" * 64,
                task_budget={"budget": {}, "usage": {}},
            )
        )

    provider = ScriptedProviderBoundary(
        {agent: tool_name for agent, (_key, tool_name) in agent_tools.items()}
    )
    model_runtime = ModelRuntime(sessions, provider_factory=lambda _provider: provider)
    trace_service = DecisionTraceService(sessions)
    loop = AgentLoop(
        registry,
        max_steps=2,
        policy_registry=policy_registry,
        decision_trace_service=trace_service,
    )
    for agent_name, (binding_key, _tool_name) in agent_tools.items():
        packet = ContextPacket(
            role=agent_name,  # type: ignore[arg-type]
            task_id=task_id,
            query=(
                "analyze user@example.com while secret=do-not-publish "
                f"for {agent_name}"
            ),
            deadline=utcnow() + timedelta(minutes=2),
            payload={
                "runtime": {"node_run_id": f"node_{suffix}_{agent_name}"},
                "branch": "focused",
            },
        )
        execution = ExecutionContext(
            tenant_id=tenant_id,
            user_id=user_id,
            task_id=task_id,
            thread_id=thread_id,
            lease_token="phase13-smoke-lease",
            cancellation_token=NeverCancelled(),
            deadline=utcnow() + timedelta(minutes=2),
            capability_snapshot=capability.model_dump(mode="json"),
        )
        answer = loop.run(
            agent_name=agent_name,
            model=model_runtime.build_chat_model_for(
                tenant_id, model_snapshot.get(binding_key), agent_name  # type: ignore[arg-type]
            ),
            packet=packet,
            execution=execution,
            policy=policies[agent_name],
            prompt="Phase 13 bounded ReAct Tool Decision smoke",
        )
        assert answer == "done"

    traces = trace_service.list_candidates(tenant_id=tenant_id, user_id=user_id)
    assert len(traces) == 6
    assert {row.agent_name for row in traces} == set(agent_tools)
    assert sum(row.execution is not None for row in traces) == 3
    assert {row.decision["selected_tool"] for row in traces} == {
        *(tool_name for _key, tool_name in agent_tools.values()),
        NO_TOOL,
    }

    governance = ToolDecisionGovernance(
        sessions,
        registry,
        TrainingPermissionPolicy(
            allowed_tenants=frozenset({tenant_id}),
            allowed_users=frozenset({(tenant_id, user_id)}),
        ),
    )
    reviews = ToolDecisionReviewService(sessions)
    label_builder = ToolLabelBuilder()
    for trace in traces:
        decision, governed = governance.evaluate(trace)
        assert decision.eligible and governed is not None
        selected = governed.decision.selected_tool
        label = label_builder.build(
            governed,
            acceptable_tools=[selected],
            argument_assertions=(
                [ArgumentAssertion(kind="required", path="query")]
                if selected != NO_TOOL
                else []
            ),
            label_source="deterministic_rule",
            confidence="high",
            difficulty="trivial",
        )
        reviews.submit(
            decision_id=trace.decision_id,
            reviewer_ref="system:phase13-real-smoke",
            disposition="accepted",
            label=label,
        )

    artifact_root = Path(tempfile.mkdtemp(prefix="deepresearch-phase13-"))
    artifact_store = FlywheelArtifactStore(FilesystemArtifactObjectStore(artifact_root))
    split_policy = next(
        policy
        for index in range(100)
        if (
            policy := ThreadSplitPolicy(
                train_percent=98, val_percent=1, seed=f"phase13-real-{index}"
            )
        ).split(thread_id)
        == "train"
    )
    dataset = ToolDatasetBuilder(
        artifact_store,
        governance,
        reviews,
        split_policy=split_policy,
    ).build(
        dataset_id="tool-decisions-real-smoke",
        version=f"v-{suffix}",
        traces=traces,
        git_commit="working-tree-phase13",
        created_at=utcnow(),
    )
    loader = FlywheelDatasetLoader(artifact_store)
    samples = loader.load_split(dataset.dataset_ref, "train")
    assert len(samples) == 6
    published = b"".join(
        artifact_store.object_store.get(ref)
        for ref in artifact_store.object_store.list_prefix(dataset.dataset_ref)
    ).decode("utf-8")
    for forbidden in (
        tenant_id,
        user_id,
        task_id,
        thread_id,
        "user@example.com",
        "do-not-publish",
    ):
        assert forbidden not in published

    evaluator = OfflineToolEvaluator()
    baseline = evaluator.evaluate(
        samples,
        lambda _context: ToolDecisionPrediction(
            selected_tool=NO_TOOL, execution_status="error"
        ),
    )
    candidate = evaluator.evaluate(samples, _perfect_prediction)
    gate = OfflineToolGate(artifact_store).compare(
        dataset_ref=dataset.dataset_ref,
        baseline=baseline,
        candidate=candidate,
        baseline_config={"binding": "production-baseline"},
        candidate_config={"binding": "tool-candidate"},
        code_revision="working-tree-phase13",
        gate_config=ToolGateConfig(
            version="tool-gate-v1", selection_delta=0.1, one_shot_delta=0.1
        ),
        created_at=utcnow(),
    )
    assert gate.passed

    training = ToolTrainingPipeline(
        loader, artifact_store, DryRunToolTrainingBackend()
    ).run(
        ToolTrainingConfig(
            config_version="phase13-smoke-v1",
            base_model="dry-run/non-production",
            dataset_ref=dataset.dataset_ref,
        ),
        code_revision="working-tree-phase13",
    )
    assert training.status == "completed" and training.metrics["dry_run"] is True

    with sessions() as session:
        persisted_trace_count = session.scalar(
            select(func.count())
            .select_from(DecisionTraceRow)
            .where(DecisionTraceRow.tenant_id == tenant_id)
        )
        persisted_review_count = session.scalar(
            select(func.count())
            .select_from(ToolDecisionReviewRow)
            .join(DecisionTraceRow)
            .where(DecisionTraceRow.tenant_id == tenant_id)
        )
    assert persisted_trace_count == 6 and persisted_review_count == 6
    print(
        json.dumps(
            {
                "postgresql": "real",
                "model_runtime": type(model_runtime).__name__,
                "provider_boundary": "deterministic_nonbillable",
                "provider_chat_calls": provider.chat_calls,
                "tool_runtime_calls": {
                    name: len(adapter.calls) for name, adapter in adapters.items()
                },
                "persisted_decision_traces": persisted_trace_count,
                "persisted_review_revisions": persisted_review_count,
                "agents": sorted({row.agent_name for row in traces}),
                "tool_decisions": 3,
                "no_tool_decisions": 3,
                "dataset_ref": dataset.dataset_ref,
                "dataset_sample_count": dataset.manifest["sample_count"],
                "artifact_root": str(artifact_root),
                "candidate_metrics": candidate.overall,
                "safety_gate": {"passed": gate.passed, "id": gate.evaluation_id},
                "training": {
                    "status": "code_only_dry_run",
                    "run_id": training.training_run_id,
                },
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
