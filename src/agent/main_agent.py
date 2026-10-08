from __future__ import annotations

import hashlib
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any, Literal, Protocol, cast

from langgraph.graph import END, START, StateGraph
from sqlalchemy import select

from src.agent.subagents.implementations import (
    ChiefArchitect,
    CodeWizard,
    CriticMaster,
    DataAnalyst,
    DeepScout,
    RouteNode,
    LeadWriter,
)
from src.agent.tools.skill_management import SkillRuntime
from src.agent.tools.database_write import PrepareWriteService
from src.agent.schema.report import ReportAst
from src.agent.schema.model_config import ModelBindingSnapshot
from src.agent.schema.task import TaskBudget
from src.agent.schema.runtime import (
    AssistantMessageEffect,
    ExecutionContext,
    NodeResult,
    PendingWriteEffect,
)
from src.agent.schema.runtime import StatePatch
from src.agent.schema.state import AnalysisResult, BranchResult, ResearchRound, ResearchState
from src.agent.schema.write import WritePlan
from src.agent.graph.evidence import BranchNotTerminalError, EvidencePipeline
from src.agent.memory.service import MemoryService
from src.agent.backends.persistence.models import NodeRunRow, ResearchCheckpointRow, ResearchTaskRow
from src.agent.graph.control import AnalysisDecision, CoverageEvaluator, DeepenController
from src.agent.runtime.agent_loop import AgentLoop
from src.agent.runtime.node_runner import NodeRunner
from src.agent.runtime.tool_runtime import ToolPolicy, ToolPolicyRegistry, ToolRegistry, ToolRuntime
from src.agent.runtime.tool_decisions import DecisionTraceService
from src.agent.reporting.reporting import CitationValidator, ReportPublisher
from src.agent.errors import LeaseLostError


class ChatRuntime(Protocol):
    def build_chat_model(self, tenant_id: str, binding: Any) -> Any: ...

    def build_chat_model_for(self, tenant_id: str, binding: Any, role: str) -> Any: ...

    def embed(self, tenant_id: str, binding: Any, texts: list[str]) -> list[list[float]]: ...


@dataclass(frozen=True)
class GraphRunResult:
    outcome: Literal[
        "success",
        "phase1_deep_planned",
        "phase2_evidence_ready",
        "phase3_research_approved",
        "paused",
        "cancelled",
        "failure",
    ]
    state: ResearchState
    checkpoint_id: str
    failure_reason: str | None = None


class ResearchGraph:
    def __init__(
        self,
        node_runner: NodeRunner,
        model_runtime: ChatRuntime,
        *,
        tool_registry: ToolRegistry | None = None,
        tool_runtime: ToolRuntime | None = None,
        tool_policies: dict[str, ToolPolicy] | None = None,
        skill_runtime: SkillRuntime | None = None,
        tool_policy_registry: ToolPolicyRegistry | None = None,
        decision_trace_service: DecisionTraceService | None = None,
        evidence_pipeline: EvidencePipeline | None = None,
        prepare_write_service: PrepareWriteService | None = None,
        citation_validator: CitationValidator | None = None,
        report_publisher: ReportPublisher | None = None,
        memory_service: MemoryService | None = None,
        phase2_enabled: bool = True,
        phase3_enabled: bool = False,
        phase4_enabled: bool = False,
        agent_max_steps: int = 6,
        retrieval_branch_concurrency: int = 3,
    ) -> None:
        self.node_runner = node_runner
        self.model_runtime = model_runtime
        self.route_node = RouteNode()
        self.chief = ChiefArchitect()
        self.analysis_decision = AnalysisDecision()
        self.coverage_evaluator = CoverageEvaluator()
        self.deepen_controller = DeepenController()
        self.critic = CriticMaster()
        self.writer = LeadWriter()
        self.evidence_pipeline = evidence_pipeline
        self.phase2_enabled = phase2_enabled
        self.phase3_enabled = phase3_enabled
        self.phase4_enabled = phase4_enabled
        self.prepare_write_service = prepare_write_service
        self.citation_validator = citation_validator
        self.report_publisher = report_publisher
        self.memory_service = memory_service
        self.retrieval_branch_concurrency = max(1, retrieval_branch_concurrency)
        registry = tool_registry or ToolRegistry()
        policies = tool_policies or {}
        loop = AgentLoop(
            registry,
            max_steps=agent_max_steps,
            tool_runtime=tool_runtime,
            skill_runtime=skill_runtime,
            policy_registry=tool_policy_registry,
            decision_trace_service=decision_trace_service,
        )
        self.focused_agents = {
            "deep_scout": DeepScout(loop, policies.get("deep_scout", ToolPolicy(frozenset()))),
            "data_analyst": DataAnalyst(
                loop, policies.get("data_analyst", ToolPolicy(frozenset()))
            ),
            "code_wizard": CodeWizard(loop, policies.get("code_wizard", ToolPolicy(frozenset()))),
        }
        self.tool_policies = policies

    def run(
        self,
        *,
        state: ResearchState,
        execution: ExecutionContext,
        budget: TaskBudget,
        bindings: ModelBindingSnapshot,
        input_checkpoint_id: str,
    ) -> GraphRunResult:
        cursor = {"checkpoint_id": input_checkpoint_id}
        terminal: dict[str, Any] = {}
        last_outputs: dict[str, dict[str, Any]] = {}
        report_flow: dict[str, Any] = {}
        delivery_failure: dict[str, str] = {}

        with self.node_runner.session_factory() as session:
            resume_checkpoint = session.get(ResearchCheckpointRow, input_checkpoint_id)
            resume_position = (
                resume_checkpoint.graph_position if resume_checkpoint is not None else "START"
            )
            resume_node_key = (
                str(resume_checkpoint.scheduling_state.get("node_key", ""))
                if resume_checkpoint is not None
                else ""
            )
            resume_run = session.scalar(
                select(NodeRunRow).where(
                    NodeRunRow.task_id == execution.task_id,
                    NodeRunRow.output_checkpoint_id == input_checkpoint_id,
                )
            )
            resume_output = (
                dict((resume_run.result_json or {}).get("output") or {})
                if resume_run is not None
                else {}
            )

        def coerce(value: ResearchState | dict[str, Any]) -> ResearchState:
            return value if isinstance(value, ResearchState) else ResearchState.model_validate(value)

        def refresh_budget() -> None:
            with self.node_runner.session_factory() as session:
                payload = session.scalar(
                    select(ResearchTaskRow.task_budget).where(
                        ResearchTaskRow.tenant_id == execution.tenant_id,
                        ResearchTaskRow.task_id == execution.task_id,
                    )
                )
            if payload is not None:
                current = TaskBudget.model_validate(payload)
                budget.budget = current.budget
                budget.usage = current.usage

        def execute(
            *,
            node_name: str,
            context_role: str,
            current: ResearchState,
            handler: Any,
            node_key: str | None = None,
            checkpoint_id: str | None = None,
            update_cursor: bool = True,
        ) -> dict[str, Any]:
            refresh_budget()
            result = self.node_runner.execute(
                node_name=node_name,
                node_key=node_key or node_name,
                context_role=context_role,
                input_checkpoint_id=checkpoint_id or cursor["checkpoint_id"],
                execution=execution,
                state=current,
                budget=budget,
                handler=handler,
            )
            assert result.checkpoint_id is not None
            if update_cursor:
                cursor["checkpoint_id"] = result.checkpoint_id
            last_outputs[node_key or node_name] = result.result.output
            return result.state

        def latest_committed() -> dict[str, Any]:
            with self.node_runner.session_factory() as session:
                task = session.scalar(
                    select(ResearchTaskRow).where(
                        ResearchTaskRow.tenant_id == execution.tenant_id,
                        ResearchTaskRow.task_id == execution.task_id,
                    )
                )
                checkpoint = session.scalar(
                    select(ResearchCheckpointRow)
                    .where(
                        ResearchCheckpointRow.tenant_id == execution.tenant_id,
                        ResearchCheckpointRow.task_id == execution.task_id,
                    )
                    .order_by(ResearchCheckpointRow.checkpoint_seq.desc())
                    .limit(1)
                )
                if task is None or checkpoint is None:
                    raise RuntimeError("Task checkpoint disappeared during retrieval fan-out")
                cursor["checkpoint_id"] = checkpoint.checkpoint_id
                return dict(task.latest_state)

        def refresh_memory(
            current: ResearchState,
            planning_query: str,
            *,
            node_name: str,
            node_key: str,
            context_role: str,
        ) -> ResearchState:
            if self.memory_service is None:
                return current
            query_hash = hashlib.sha256(planning_query.encode("utf-8")).hexdigest()
            if current.memory_context and current.memory_context.query_hash == query_hash:
                return current
            query_embedding: list[float] | None = None
            embedding_binding = bindings.bindings.get("embedding")
            if embedding_binding is not None:
                try:
                    query_embedding = self.model_runtime.embed(
                        execution.tenant_id, embedding_binding, [planning_query]
                    )[0]
                except Exception:
                    # Embedding is an optional recall route; topic/base/lexical remain available.
                    query_embedding = None
            snapshot = self.memory_service.recall(
                tenant_id=execution.tenant_id,
                user_id=execution.user_id,
                planning_query=planning_query,
                query_embedding=query_embedding,
            )
            committed = execute(
                node_name=node_name,
                context_role=context_role,
                current=current,
                handler=lambda _packet, _attempt: NodeResult(
                    patch=StatePatch(
                        values={"memory_context": snapshot.model_dump(mode="json")}
                    )
                ),
                node_key=node_key,
            )
            return coerce(committed)

        def deepen_planning_query(current: ResearchState) -> str:
            parts = [current.query]
            if current.coverage and not current.coverage.complete:
                parts.extend(item.description for item in current.coverage.gaps)
            if current.critic and current.critic.decision != "pass":
                parts.extend(item.reason for item in current.critic.gaps)
            if current.plan:
                parts.extend(
                    query.text
                    for hypothesis in current.plan.hypotheses
                    for query in hypothesis.queries
                )
            return "\n".join(item for item in parts if item)

        def chat_model(binding_key: str, role: str) -> Any:
            binding = bindings.get(binding_key)  # type: ignore[arg-type]
            build_for = getattr(self.model_runtime, "build_chat_model_for", None)
            if build_for is not None:
                return build_for(execution.tenant_id, binding, role)
            return self.model_runtime.build_chat_model(execution.tenant_id, binding)

        def route_node(value: ResearchState | dict[str, Any]) -> dict[str, Any]:
            current = coerce(value)
            model = chat_model("routing", "route")
            return execute(
                node_name="route",
                context_role="route",
                current=current,
                handler=lambda packet, _attempt: self.route_node.run(packet, model),
            )

        def choose_route(value: ResearchState | dict[str, Any]) -> str:
            current = coerce(value)
            if current.route is None:
                raise RuntimeError("Route node did not commit a decision")
            return current.route.mode

        def can_continue(current: ResearchState) -> bool:
            refresh_budget()
            return (
                current.execution.depth < current.execution.max_depth
                and not self.coverage_evaluator._budget_exhausted(budget)
            )

        def route_coverage(current: ResearchState) -> str:
            if current.coverage is None:
                return "coverage"
            if current.coverage.complete:
                return "critic"
            if current.coverage.stop_reason is not None or not can_continue(current):
                return "failure"
            return "deepen"

        def route_critic(current: ResearchState) -> str:
            if current.critic is None:
                return "critic"
            if current.critic.decision == "pass":
                return "delivery" if self.phase4_enabled else "boundary"
            if not can_continue(current):
                return "failure"
            return current.critic.decision

        def choose_start(value: ResearchState | dict[str, Any]) -> str:
            current = coerce(value)
            if resume_position == "memory_recall_plan":
                return "plan"
            if resume_position == "route" and current.route is not None:
                return {
                    "direct": "direct",
                    "focused": "focused_dispatch",
                    "deep": "plan",
                }[current.route.mode]
            if resume_position == "focused_dispatch":
                assert current.route is not None and current.route.target_agent is not None
                return f"focused_{current.route.target_agent.value}"
            if resume_position in {
                "chief_direct",
                "deep_scout",
                "data_analyst",
                "code_wizard",
            } and current.route is not None and current.route.mode in {"direct", "focused"}:
                return "success"
            if not (
                self.phase2_enabled
                and current.route is not None
                and current.route.mode == "deep"
                and current.plan is not None
            ):
                return "route"

            round_ = current.research_rounds.get(str(current.execution.depth))
            if not self.phase3_enabled:
                if round_ is None or any(
                    branch.status == "pending"
                    for branch in (round_.web, round_.rag, round_.sql)
                ):
                    return "dispatch"
                return "join"

            if resume_position == "phase3_boundary":
                return "delivery" if self.phase4_enabled else "boundary"
            if self.phase4_enabled:
                if resume_position in {"write_rejected", "write_terminal"}:
                    return "writer"
                if resume_position == "write_stale":
                    return "delivery"
                if resume_position == "database_write_gate":
                    return "prepare" if resume_output.get("requires_write") else "writer"
                if resume_position == "prepare_write":
                    return "paused"
                if resume_position == "lead_writer":
                    report_flow.update(resume_output)
                    return "validate"
                if resume_position == "revise":
                    report_flow.update(resume_output)
                    return "validate"
                if resume_position == "validate_report":
                    report_flow.update(resume_output)
                    if resume_output.get("valid"):
                        return "finalize"
                    return (
                        "revise"
                        if current.execution.revision_round
                        < current.execution.max_revision_rounds
                        else "failure"
                    )
                if resume_position == "finalize":
                    return "graph_success"
                if resume_position == "graph_success":
                    return "success"
            if resume_position == "graph_failure":
                return "failure"
            if resume_position == "merge_evidence":
                return "coverage" if ":compute:" in resume_node_key else "analysis"
            if resume_position == "analysis_decision":
                return (
                    "compute"
                    if any(item.status == "pending" for item in current.analysis_requests)
                    else "coverage"
                )
            if resume_position == "code_wizard_compute":
                return (
                    "compute"
                    if any(item.status == "pending" for item in current.analysis_requests)
                    else "merge_compute"
                )
            if resume_position == "coverage":
                return route_coverage(current)
            if resume_position == "memory_recall_deepen":
                return "deepen"
            if resume_position == "chief_deepen":
                return "failure" if resume_output.get("no_new_queries") else "dispatch"
            if resume_position == "critic":
                return route_critic(current)
            if resume_position == "retrieval_join":
                return "merge_retrieval"
            if resume_position in {
                "retrieval_dispatch",
                "deep_scout_web",
                "deep_scout_rag",
                "data_analyst_sql",
            }:
                if round_ is None or any(
                    branch.status == "pending"
                    for branch in (round_.web, round_.rag, round_.sql)
                ):
                    return "dispatch"
                return "join"
            if resume_position == "chief_plan" or round_ is None:
                return "dispatch"
            if any(item.status == "pending" for item in current.analysis_requests):
                return "compute"
            if current.coverage is not None:
                return route_coverage(current)
            return "join"

        def chief_direct(value: ResearchState | dict[str, Any]) -> dict[str, Any]:
            current = coerce(value)
            model = chat_model("reasoning", "chief_direct")
            result = execute(
                node_name="chief_direct",
                context_role="chief_direct",
                current=current,
                handler=lambda packet, _attempt: self.chief.direct(packet, model),
            )
            terminal["outcome"] = "success"
            return result

        def focused_dispatch(value: ResearchState | dict[str, Any]) -> dict[str, Any]:
            current = coerce(value)
            assert current.route is not None and current.route.target_agent is not None
            return execute(
                node_name="focused_dispatch",
                context_role="route",
                current=current,
                handler=lambda _packet, _attempt: NodeResult(
                    output={"target_agent": current.route.target_agent.value}
                ),
            )

        def choose_focused(value: ResearchState | dict[str, Any]) -> str:
            current = coerce(value)
            assert current.route is not None and current.route.target_agent is not None
            return current.route.target_agent.value

        def focused(name: str, binding_key: str):
            def run(value: ResearchState | dict[str, Any]) -> dict[str, Any]:
                current = coerce(value)
                model = chat_model(binding_key, name)
                result = execute(
                    node_name=name,
                    context_role=name,
                    current=current,
                    handler=lambda packet, _attempt: self.focused_agents[name].run(
                        packet, execution, model
                    ),
                )
                terminal["outcome"] = "success"
                return result

            return run

        def chief_plan(value: ResearchState | dict[str, Any]) -> dict[str, Any]:
            current = coerce(value)
            current = refresh_memory(
                current,
                current.query,
                node_name="memory_recall_plan",
                node_key=f"memory_recall_plan:{hashlib.sha256(current.query.encode()).hexdigest()}",
                context_role="chief_plan",
            )
            model = chat_model("reasoning", "chief_plan")
            result = execute(
                node_name="chief_plan",
                context_role="chief_plan",
                current=current,
                handler=lambda packet, _attempt: self.chief.plan(packet, model),
            )
            if not self.phase2_enabled:
                terminal["outcome"] = "phase1_deep_planned"
            return result

        def retrieval_channels(
            current: ResearchState,
        ) -> set[Literal["web", "rag", "sql"]]:
            if current.critic is not None and current.critic.decision == "retrieve":
                return {
                    gap.target_channel
                    for gap in current.critic.gaps
                    if gap.target_channel is not None
                }
            assert current.plan is not None
            if current.execution.depth == 0:
                deepened = {
                    query_id
                    for record in current.plan.deepen_history
                    for query_id in record.added_query_ids
                }
                return {
                    query.channel
                    for hypothesis in current.plan.hypotheses
                    for query in hypothesis.queries
                    if query.query_id not in deepened
                }
            record = next(
                (
                    item
                    for item in current.plan.deepen_history
                    if item.depth == current.execution.depth
                ),
                None,
            )
            query_ids = set(record.added_query_ids) if record else set()
            return {
                query.channel
                for hypothesis in current.plan.hypotheses
                for query in hypothesis.queries
                if query.query_id in query_ids
            }

        def retrieval_dispatch(value: ResearchState | dict[str, Any]) -> dict[str, Any]:
            current = coerce(value)
            if current.plan is None:
                raise RuntimeError("RetrievalDispatch requires a plan")
            depth = str(current.execution.depth)
            rounds = dict(current.research_rounds)
            channels = retrieval_channels(current)
            if depth not in rounds:
                rounds[depth] = ResearchRound(
                    web=BranchResult(
                        status="pending" if "web" in channels else "skipped"
                    ),
                    rag=BranchResult(
                        status="pending" if "rag" in channels else "skipped"
                    ),
                    sql=BranchResult(
                        status="pending" if "sql" in channels else "skipped"
                    ),
                )
                patch = StatePatch(
                    values={
                        "research_rounds": {
                            key: item.model_dump(mode="json")
                            for key, item in rounds.items()
                        }
                    }
                )
            else:
                patch = StatePatch()
            dispatched_payload = execute(
                node_name="retrieval_dispatch",
                context_role="chief_plan",
                current=current,
                handler=lambda _packet, _attempt: NodeResult(patch=patch),
            )
            dispatched = coerce(dispatched_payload)
            current_round = dispatched.research_rounds[str(dispatched.execution.depth)]
            critic_retrieve = (
                dispatched.critic is not None
                and dispatched.critic.decision == "retrieve"
            )
            retrieval_order: tuple[
                Literal["web", "rag", "sql"],
                Literal["web", "rag", "sql"],
                Literal["web", "rag", "sql"],
            ] = ("web", "rag", "sql")
            scheduled = (
                sorted(channels)
                if critic_retrieve
                else [
                    branch
                    for branch in retrieval_order
                    if getattr(current_round, branch).status == "pending"
                ]
            )
            if not scheduled:
                return dispatched_payload
            branch_checkpoint = cursor["checkpoint_id"]

            def recovery_checkpoint(branch: str) -> str:
                node_name = {
                    "web": "deep_scout_web",
                    "rag": "deep_scout_rag",
                    "sql": "data_analyst_sql",
                }[branch]
                with self.node_runner.session_factory() as session:
                    unfinished = session.scalar(
                        select(NodeRunRow)
                        .where(
                            NodeRunRow.task_id == execution.task_id,
                            NodeRunRow.node_key == node_name,
                            NodeRunRow.status.in_(["pending", "running", "failed"]),
                        )
                        .order_by(NodeRunRow.updated_at.desc())
                        .limit(1)
                    )
                    return (
                        unfinished.input_checkpoint_id
                        if unfinished is not None
                        else branch_checkpoint
                    )

            with ThreadPoolExecutor(
                max_workers=min(self.retrieval_branch_concurrency, len(scheduled)),
                thread_name_prefix="retrieval",
            ) as executor:
                futures = [
                    executor.submit(
                        retrieval_branch(branch),
                        dispatched,
                        recovery_checkpoint(branch),
                    )
                    for branch in scheduled
                ]
                for future in futures:
                    future.result()
            return latest_committed()

        def retrieval_branch(branch: Literal["web", "rag", "sql"]):
            node_name = {
                "web": "deep_scout_web",
                "rag": "deep_scout_rag",
                "sql": "data_analyst_sql",
            }[branch]
            agent_name = "deep_scout" if branch in {"web", "rag"} else "data_analyst"
            binding_key = "tool" if branch in {"web", "rag"} else "data"

            def run(
                value: ResearchState | dict[str, Any], branch_checkpoint: str
            ) -> dict[str, Any]:
                current = coerce(value)
                model = chat_model(binding_key, node_name)

                def handler(packet: Any, _attempt: int) -> NodeResult:
                    target = getattr(
                        current.research_rounds[str(current.execution.depth)], branch
                    )
                    try:
                        adopted = self.focused_agents[agent_name].retrieve(
                            packet, execution, model, branch
                        )
                        combined = {
                            (item.hypothesis_id, item.result_key): item
                            for item in target.items
                        }
                        for item in adopted:
                            combined[(item.hypothesis_id, item.result_key)] = item
                        replacement = BranchResult(
                            status=(
                                "failed" if target.status == "failed" else "completed"
                            ),
                            items=list(combined.values()),
                            error=target.error if target.status == "failed" else None,
                        )
                    except Exception as exc:
                        replacement = BranchResult(
                            status="failed",
                            items=target.items,
                            error={"type": type(exc).__name__, "message": str(exc)[:500]},
                        )
                    return NodeResult(
                        patch=StatePatch(
                            branch_updates={
                                str(current.execution.depth): {
                                    branch: replacement.model_dump(mode="json")
                                }
                            }
                        ),
                        output={
                            "agent_trace": self.focused_agents[
                                agent_name
                            ].renderer.trace(
                                self.focused_agents[agent_name].prompt_name,
                                packet,
                                model,
                            )
                        },
                    )

                return execute(
                    node_name=node_name,
                    context_role=node_name,
                    current=current,
                    handler=handler,
                    checkpoint_id=branch_checkpoint,
                    update_cursor=False,
                )

            return run

        def retrieval_join(value: ResearchState | dict[str, Any]) -> dict[str, Any]:
            current = coerce(value)

            def handler(_packet: Any, _attempt: int) -> NodeResult:
                round_ = current.research_rounds[str(current.execution.depth)]
                if any(
                    branch.status == "pending"
                    for branch in (round_.web, round_.rag, round_.sql)
                ):
                    raise BranchNotTerminalError("RetrievalJoin observed a pending branch")
                return NodeResult(output={"depth": current.execution.depth})

            return execute(
                node_name="retrieval_join",
                context_role="chief_plan",
                current=current,
                handler=handler,
            )

        def merge_evidence(
            value: ResearchState | dict[str, Any], *, stage: Literal["retrieval", "compute"]
        ) -> dict[str, Any]:
            current = coerce(value)
            evidence_pipeline = self.evidence_pipeline
            if evidence_pipeline is None:
                raise RuntimeError("EvidencePipeline is not configured")

            def handler(_packet: Any, _attempt: int) -> NodeResult:
                merged = evidence_pipeline.merge(current)
                additions = merged[len(current.evidence_pool) :]
                return NodeResult(
                    patch=StatePatch(
                        list_appends={
                            "evidence_pool": [
                                item.model_dump(mode="json") for item in additions
                            ]
                        }
                    ),
                    output={
                        "evidence_count": len(merged),
                        "evidence_added": len(additions),
                    },
                )

            result = execute(
                node_name="merge_evidence",
                context_role="chief_plan",
                current=current,
                handler=handler,
                node_key=f"merge_evidence:{stage}:{current.execution.depth}",
            )
            if not self.phase3_enabled:
                terminal["outcome"] = "phase2_evidence_ready"
            return result

        def post_merge_analysis(value: ResearchState | dict[str, Any]) -> dict[str, Any]:
            current = coerce(value)
            model = chat_model("reasoning", "analysis_decision")
            return execute(
                node_name="analysis_decision",
                context_role="analysis_decision",
                current=current,
                handler=lambda packet, _attempt: self.analysis_decision.run(
                    packet, model, current
                ),
                node_key=f"analysis_decision:{current.execution.depth}",
            )

        def choose_analysis(value: ResearchState | dict[str, Any]) -> str:
            current = coerce(value)
            return (
                "compute"
                if any(item.status == "pending" for item in current.analysis_requests)
                else "coverage"
            )

        def compute_requests(value: ResearchState | dict[str, Any]) -> dict[str, Any]:
            payload = value.model_dump(mode="json") if isinstance(value, ResearchState) else value
            while True:
                current = coerce(payload)
                request = next(
                    (item for item in current.analysis_requests if item.status == "pending"),
                    None,
                )
                if request is None:
                    return payload
                model = chat_model("code", "code_wizard_compute")

                def handler(packet: Any, _attempt: int) -> NodeResult:
                    round_ = current.research_rounds[str(current.execution.depth)]
                    target = round_.compute or BranchResult(status="pending")
                    adopted = []
                    try:
                        adopted = cast(
                            CodeWizard, self.focused_agents["code_wizard"]
                        ).compute(
                            packet, execution, model
                        )
                        if not adopted:
                            raise ValueError("CodeWizard did not adopt a compute result")
                        combined = {
                            (item.hypothesis_id, item.result_key): item
                            for item in target.items
                        }
                        for item in adopted:
                            combined[(item.hypothesis_id, item.result_key)] = item
                        replacement = BranchResult(
                            status=(
                                "failed" if target.status == "failed" else "completed"
                            ),
                            items=list(combined.values()),
                            error=target.error if target.status == "failed" else None,
                        )
                        status: Literal["completed", "failed"] = "completed"
                        error: dict[str, str] | None = None
                    except Exception as exc:
                        error = {
                            "type": type(exc).__name__,
                            "message": str(exc)[:500],
                        }
                        replacement = BranchResult(
                            status="failed", items=target.items, error=error
                        )
                        status = "failed"
                    analysis_result = AnalysisResult(
                        analysis_id=request.analysis_id,
                        status=status,
                        adopted_results=adopted,
                        error=error,
                    )
                    return NodeResult(
                        patch=StatePatch(
                            branch_updates={
                                str(current.execution.depth): {
                                    "compute": replacement.model_dump(mode="json")
                                }
                            },
                            analysis_status_updates={request.analysis_id: status},
                        ),
                        output={
                            "analysis_id": request.analysis_id,
                            "status": status,
                            "analysis_result": analysis_result.model_dump(mode="json"),
                            **({"error": error} if error else {}),
                        },
                    )

                payload = execute(
                    node_name="code_wizard_compute",
                    context_role="code_wizard_compute",
                    current=current,
                    handler=handler,
                    node_key=f"code_wizard_compute:{request.analysis_id}",
                )

        def coverage(value: ResearchState | dict[str, Any]) -> dict[str, Any]:
            current = coerce(value)
            return execute(
                node_name="coverage",
                context_role="coverage",
                current=current,
                handler=lambda _packet, _attempt: NodeResult(
                    patch=StatePatch(
                        values={
                            "coverage": self.coverage_evaluator.evaluate(
                                current, budget
                            ).model_dump(mode="json")
                        }
                    )
                ),
                node_key=f"coverage:{current.execution.depth}:{len(current.evidence_pool)}",
            )

        def choose_coverage(value: ResearchState | dict[str, Any]) -> str:
            return route_coverage(coerce(value))

        deepen_status: dict[str, Any] = {}

        def deepen(value: ResearchState | dict[str, Any]) -> dict[str, Any]:
            current = coerce(value)
            planning_query = deepen_planning_query(current)
            current = refresh_memory(
                current,
                planning_query,
                node_name="memory_recall_deepen",
                node_key=(
                    f"memory_recall_deepen:{current.execution.depth + 1}:"
                    f"{hashlib.sha256(planning_query.encode()).hexdigest()}"
                ),
                context_role="chief_deepen",
            )
            model = chat_model("reasoning", "chief_deepen")
            node_key = f"chief_deepen:{current.execution.depth + 1}"
            result = execute(
                node_name="chief_deepen",
                context_role="chief_deepen",
                current=current,
                handler=lambda packet, _attempt: self.deepen_controller.run(
                    packet, model, current
                ),
                node_key=node_key,
            )
            deepen_status.clear()
            deepen_status.update(last_outputs.get(node_key, {}))
            return result

        def choose_deepen(_value: ResearchState | dict[str, Any]) -> str:
            return "failure" if deepen_status.get("no_new_queries") else "dispatch"

        def critic(value: ResearchState | dict[str, Any]) -> dict[str, Any]:
            current = coerce(value)
            if current.coverage is None or not current.coverage.complete:
                raise RuntimeError("Critic requires complete deterministic Coverage")
            model = chat_model("reasoning", "critic")
            return execute(
                node_name="critic",
                context_role="critic",
                current=current,
                handler=lambda packet, _attempt: self.critic.review(packet, model),
                node_key=f"critic:{current.execution.depth}:{len(current.evidence_pool)}",
            )

        def choose_critic(value: ResearchState | dict[str, Any]) -> str:
            return route_critic(coerce(value))

        def phase3_boundary(value: ResearchState | dict[str, Any]) -> dict[str, Any]:
            current = coerce(value)
            if current.critic is None or current.critic.decision != "pass":
                raise RuntimeError("Phase 3 boundary requires an approved CriticResult")
            result = execute(
                node_name="phase3_boundary",
                context_role="phase3_boundary",
                current=current,
                handler=lambda _packet, _attempt: NodeResult(
                    output={"research_approved": True, "score": current.critic.score}
                ),
                node_key="phase3_boundary",
            )
            terminal["outcome"] = "phase3_research_approved"
            return result

        def database_write_gate(value: ResearchState | dict[str, Any]) -> dict[str, Any]:
            current = coerce(value)
            if (
                current.critic is None
                or current.critic.decision != "pass"
                or current.critic.score < 8
            ):
                raise RuntimeError("DatabaseWriteGate requires Critic approval")
            requires_write = bool(current.plan and current.plan.write_intent)
            return execute(
                node_name="database_write_gate",
                context_role="database_write_gate",
                current=current,
                handler=lambda _packet, _attempt: NodeResult(
                    output={"requires_write": requires_write}
                ),
                node_key="database_write_gate",
            )

        def choose_database_write(value: ResearchState | dict[str, Any]) -> str:
            current = coerce(value)
            return "prepare" if current.plan and current.plan.write_intent else "writer"

        def prepare_write(value: ResearchState | dict[str, Any]) -> dict[str, Any]:
            current = coerce(value)
            prepare_write_service = self.prepare_write_service
            if prepare_write_service is None:
                raise RuntimeError("PrepareWriteService is not configured")
            if current.plan is None or current.plan.write_intent is None:
                raise RuntimeError("PrepareWrite requires a write_intent")
            analyst = self.focused_agents["data_analyst"]
            assert isinstance(analyst, DataAnalyst)
            model = chat_model("data", "prepare_write")

            def handler(packet: Any, _attempt: int) -> NodeResult:
                plan: WritePlan = analyst.prepare_write(packet, model)
                prepared = prepare_write_service.prepare(
                    tenant_id=execution.tenant_id,
                    user_id=execution.user_id,
                    task_id=execution.task_id,
                    plan=plan,
                )
                write_id = prepared.write_id
                payload = {
                    "write_id": write_id,
                    "plan": prepared.plan.model_dump(mode="json"),
                    "plan_hash": prepared.plan_hash,
                    "preview": prepared.preview.model_dump(mode="json"),
                    "policy_revision": prepared.policy_revision,
                    "operation_key": prepared.operation_key,
                }
                return NodeResult(
                    patch=StatePatch(values={"pending_write": write_id}),
                    effects=[PendingWriteEffect(pending_write=payload)],
                    output={"write_id": write_id, "plan_hash": prepared.plan_hash},
                )

            result = execute(
                node_name="prepare_write",
                context_role="prepare_write",
                current=current,
                handler=handler,
                node_key=f"prepare_write:{current.plan.write_intent.intent_id}",
            )
            terminal["outcome"] = "paused"
            return result

        def lead_writer(value: ResearchState | dict[str, Any]) -> dict[str, Any]:
            current = coerce(value)
            model = chat_model("writing", "lead_writer")

            def handler(packet: Any, _attempt: int) -> NodeResult:
                candidate = self.writer.write(packet, model)
                payload = candidate.model_dump(mode="json")
                report_flow["candidate"] = payload
                return NodeResult(
                    output={
                        "candidate": payload,
                        "agent_trace": self.writer.renderer.trace(
                            "lead_writer.jinja2", packet, model
                        ),
                    }
                )

            return execute(
                node_name="lead_writer",
                context_role="lead_writer",
                current=current,
                handler=handler,
                node_key="lead_writer",
            )

        def validate_report(value: ResearchState | dict[str, Any]) -> dict[str, Any]:
            current = coerce(value)
            citation_validator = self.citation_validator
            report_publisher = self.report_publisher
            if citation_validator is None or report_publisher is None:
                raise RuntimeError("Report delivery services are not configured")
            candidate_payload = report_flow.get("candidate") or resume_output.get("candidate")
            if not isinstance(candidate_payload, dict):
                raise RuntimeError("ValidateReport requires a ReportAst candidate")

            def handler(_packet: Any, _attempt: int) -> NodeResult:
                candidate = ReportAst.model_validate(candidate_payload)
                validation = citation_validator.validate(
                    candidate,
                    current,
                    tenant_id=execution.tenant_id,
                    user_id=execution.user_id,
                    task_id=execution.task_id,
                )
                output = {
                    "candidate": candidate.model_dump(mode="json"),
                    "valid": validation.valid,
                    "issues": [item.model_dump(mode="json") for item in validation.issues],
                }
                report_flow.clear()
                report_flow.update(output)
                if not validation.valid:
                    return NodeResult(output=output)
                published = report_publisher.publish(
                    tenant_id=execution.tenant_id,
                    task_id=execution.task_id,
                    report=candidate,
                    state=current,
                )
                report = current.report.model_copy(deep=True)
                report.draft = published.artifact
                return NodeResult(
                    patch=StatePatch(values={"report": report.model_dump(mode="json")}),
                    output={**output, "candidate": published.ast.model_dump(mode="json")},
                )

            return execute(
                node_name="validate_report",
                context_role="validate_report",
                current=current,
                handler=handler,
                node_key=f"validate_report:{current.execution.revision_round}",
            )

        def choose_validation(value: ResearchState | dict[str, Any]) -> str:
            current = coerce(value)
            if report_flow.get("valid"):
                return "finalize"
            if current.execution.revision_round < current.execution.max_revision_rounds:
                return "revise"
            delivery_failure["reason"] = "report_validation_failed"
            return "failure"

        def revise(value: ResearchState | dict[str, Any]) -> dict[str, Any]:
            current = coerce(value)
            candidate = report_flow.get("candidate") or resume_output.get("candidate")
            issues = report_flow.get("issues") or resume_output.get("issues") or []
            if not isinstance(candidate, dict):
                raise RuntimeError("Revise requires a report candidate")
            model = chat_model("writing", "revise")

            def handler(packet: Any, _attempt: int) -> NodeResult:
                packet.payload.update({"candidate": candidate, "validation_issues": issues})
                replacement = self.writer.revise(packet, model)
                execution_state = current.execution.model_copy(
                    update={"revision_round": current.execution.revision_round + 1}
                )
                output = {
                    "candidate": replacement.model_dump(mode="json"),
                    "agent_trace": self.writer.renderer.trace(
                        "lead_writer_revise.jinja2", packet, model
                    ),
                }
                report_flow.clear()
                report_flow.update(output)
                return NodeResult(
                    patch=StatePatch(
                        values={"execution": execution_state.model_dump(mode="json")}
                    ),
                    output=output,
                )

            return execute(
                node_name="revise",
                context_role="revise",
                current=current,
                handler=handler,
                node_key=f"revise:{current.execution.revision_round + 1}",
            )

        def finalize(value: ResearchState | dict[str, Any]) -> dict[str, Any]:
            current = coerce(value)
            if current.report.draft is None:
                raise RuntimeError("Finalize requires a validated draft")
            draft = current.report.draft
            assert draft is not None
            report = current.report.model_copy(deep=True)
            report.final = draft.model_copy(deep=True)
            result = execute(
                node_name="finalize",
                context_role="finalize",
                current=current,
                handler=lambda _packet, _attempt: NodeResult(
                    patch=StatePatch(values={"report": report.model_dump(mode="json")}),
                    output={"final": report.final.model_dump(mode="json")},
                ),
                node_key=f"finalize:{report.final.version}",
            )
            terminal["outcome"] = "success"
            return result

        def graph_success(value: ResearchState | dict[str, Any]) -> dict[str, Any]:
            current = coerce(value)
            if current.report.final is None:
                raise RuntimeError("GraphSuccess requires a finalized report")
            result = execute(
                node_name="graph_success",
                context_role="graph_success",
                current=current,
                handler=lambda _packet, _attempt: NodeResult(
                    effects=(
                        [
                            AssistantMessageEffect(
                                content=(
                                    "Research report is ready: "
                                    f"{current.report.final.workspace_path}"
                                )
                            )
                        ]
                        if self.memory_service is not None
                        else []
                    ),
                    output={"report_final_ready": current.report.final is not None},
                ),
                node_key="graph_success",
            )
            terminal["outcome"] = "success"
            return result

        def resume_success(value: ResearchState | dict[str, Any]) -> dict[str, Any]:
            terminal["outcome"] = "success"
            return coerce(value).model_dump(mode="json")

        def graph_failure(value: ResearchState | dict[str, Any]) -> dict[str, Any]:
            current = coerce(value)
            refresh_budget()
            reason = delivery_failure.get("reason")
            if reason is None:
                reason = (
                    current.coverage.stop_reason
                    if current.coverage is not None and current.coverage.stop_reason
                    else "no_new_queries"
                    if deepen_status.get("no_new_queries")
                    or resume_output.get("no_new_queries")
                    else "budget_exhausted"
                    if self.coverage_evaluator._budget_exhausted(budget)
                    else "max_depth"
                    if current.execution.depth >= current.execution.max_depth
                    else "critic_gap_with_stop_condition"
                )
            reliability = current.reliability.model_copy(deep=True)
            reliability.errors.append(
                {
                    "type": (
                        "ReportValidationFailure"
                        if reason == "report_validation_failed"
                        else "NodeExecutionFailure"
                        if reason.endswith("_failed")
                        else "ResearchQualityFailure"
                    ),
                    "reason": reason,
                    "depth": current.execution.depth,
                }
            )
            result = execute(
                node_name="graph_failure",
                context_role="graph_failure",
                current=current,
                handler=lambda _packet, _attempt: NodeResult(
                    patch=StatePatch(
                        values={"reliability": reliability.model_dump(mode="json")}
                    ),
                    output={"reason": reason},
                ),
                node_key=f"graph_failure:{current.execution.depth}:{reason}",
            )
            terminal["outcome"] = "failure"
            terminal["failure_reason"] = reason
            return result

        def failed_node_reason() -> str:
            with self.node_runner.session_factory() as session:
                failed = session.scalar(
                    select(NodeRunRow)
                    .where(
                        NodeRunRow.task_id == execution.task_id,
                        NodeRunRow.status == "failed",
                    )
                    .order_by(NodeRunRow.updated_at.desc())
                    .limit(1)
                )
            if failed is None:
                return "graph_execution_failed"
            return {
                "route": "route_structured_output_failed",
                "chief_plan": "plan_structured_output_failed",
                "analysis_decision": "analysis_decision_failed",
                "chief_deepen": "deepen_failed",
                "critic": "critic_failed",
                "lead_writer": "writer_failed",
                "revise": "report_revision_failed",
                "validate_report": "report_publication_failed",
                "finalize": "finalize_failed",
            }.get(failed.node_name, f"{failed.node_name}_failed")

        builder = StateGraph(ResearchState)
        builder.add_node("Route", route_node)
        builder.add_node("Direct", chief_direct)
        builder.add_node("FocusedDispatch", focused_dispatch)
        builder.add_node("DeepScout", focused("deep_scout", "tool"))
        builder.add_node("DataAnalyst", focused("data_analyst", "data"))
        builder.add_node("CodeWizard", focused("code_wizard", "code"))
        builder.add_node("Plan", chief_plan)
        builder.add_node("ResumeSuccess", resume_success)
        if self.phase2_enabled:
            builder.add_node("RetrievalDispatch", retrieval_dispatch)
            builder.add_node("RetrievalJoin", retrieval_join)
            builder.add_node(
                "MergeEvidence",
                lambda value: merge_evidence(value, stage="retrieval"),
            )
        if self.phase3_enabled:
            builder.add_node("AnalysisDecision", post_merge_analysis)
            builder.add_node("Compute", compute_requests)
            builder.add_node(
                "MergeComputeEvidence",
                lambda value: merge_evidence(value, stage="compute"),
            )
            builder.add_node("Coverage", coverage)
            builder.add_node("Deepen", deepen)
            builder.add_node("Critic", critic)
            if self.phase4_enabled:
                builder.add_node("DatabaseWriteGate", database_write_gate)
                builder.add_node("PrepareWrite", prepare_write)
                builder.add_node("LeadWriter", lead_writer)
                builder.add_node("ValidateReport", validate_report)
                builder.add_node("Revise", revise)
                builder.add_node("Finalize", finalize)
                builder.add_node("GraphSuccess", graph_success)
            else:
                builder.add_node("Phase3Boundary", phase3_boundary)
            builder.add_node("GraphFailure", graph_failure)
        if self.phase2_enabled:
            start_targets: dict[Any, str] = {
                "route": "Route",
                "direct": "Direct",
                "focused_dispatch": "FocusedDispatch",
                "focused_deep_scout": "DeepScout",
                "focused_data_analyst": "DataAnalyst",
                "focused_code_wizard": "CodeWizard",
                "plan": "Plan",
                "dispatch": "RetrievalDispatch",
                "join": "RetrievalJoin",
                "success": "ResumeSuccess",
            }
            if self.phase3_enabled:
                start_targets.update(
                    {
                        "merge_retrieval": "MergeEvidence",
                        "analysis": "AnalysisDecision",
                        "compute": "Compute",
                        "merge_compute": "MergeComputeEvidence",
                        "coverage": "Coverage",
                        "critic": "Critic",
                        "deepen": "Deepen",
                        "retrieve": "RetrievalDispatch",
                        "failure": "GraphFailure",
                    }
                )
                if self.phase4_enabled:
                    start_targets.update(
                        {
                            "delivery": "DatabaseWriteGate",
                            "prepare": "PrepareWrite",
                            "writer": "LeadWriter",
                            "validate": "ValidateReport",
                            "revise": "Revise",
                            "finalize": "Finalize",
                            "graph_success": "GraphSuccess",
                            "paused": "PrepareWrite",
                        }
                    )
                else:
                    start_targets["boundary"] = "Phase3Boundary"
            builder.add_conditional_edges(
                START,
                choose_start,
                start_targets,
            )
        else:
            builder.add_edge(START, "Route")
        builder.add_conditional_edges(
            "Route",
            choose_route,
            {"direct": "Direct", "focused": "FocusedDispatch", "deep": "Plan"},
        )
        builder.add_conditional_edges(
            "FocusedDispatch",
            choose_focused,
            {
                "deep_scout": "DeepScout",
                "data_analyst": "DataAnalyst",
                "code_wizard": "CodeWizard",
            },
        )
        for node in ("Direct", "DeepScout", "DataAnalyst", "CodeWizard"):
            builder.add_edge(node, END)
        if self.phase2_enabled:
            builder.add_edge("Plan", "RetrievalDispatch")
            builder.add_edge("RetrievalDispatch", "RetrievalJoin")
            builder.add_edge("RetrievalJoin", "MergeEvidence")
            if self.phase3_enabled:
                builder.add_edge("MergeEvidence", "AnalysisDecision")
                builder.add_conditional_edges(
                    "AnalysisDecision",
                    choose_analysis,
                    {"compute": "Compute", "coverage": "Coverage"},
                )
                builder.add_edge("Compute", "MergeComputeEvidence")
                builder.add_edge("MergeComputeEvidence", "Coverage")
                builder.add_conditional_edges(
                    "Coverage",
                    choose_coverage,
                    {
                        "coverage": "Coverage",
                        "critic": "Critic",
                        "deepen": "Deepen",
                        "failure": "GraphFailure",
                    },
                )
                builder.add_conditional_edges(
                    "Deepen",
                    choose_deepen,
                    {"dispatch": "RetrievalDispatch", "failure": "GraphFailure"},
                )
                critic_targets: dict[Any, str] = {
                    "deepen": "Deepen",
                    "retrieve": "RetrievalDispatch",
                    "failure": "GraphFailure",
                }
                critic_targets[
                    "delivery" if self.phase4_enabled else "boundary"
                ] = "DatabaseWriteGate" if self.phase4_enabled else "Phase3Boundary"
                builder.add_conditional_edges("Critic", choose_critic, critic_targets)
                if self.phase4_enabled:
                    builder.add_conditional_edges(
                        "DatabaseWriteGate",
                        choose_database_write,
                        {"prepare": "PrepareWrite", "writer": "LeadWriter"},
                    )
                    builder.add_edge("PrepareWrite", END)
                    builder.add_edge("LeadWriter", "ValidateReport")
                    builder.add_conditional_edges(
                        "ValidateReport",
                        choose_validation,
                        {"revise": "Revise", "finalize": "Finalize", "failure": "GraphFailure"},
                    )
                    builder.add_edge("Revise", "ValidateReport")
                    builder.add_edge("Finalize", "GraphSuccess")
                    builder.add_edge("GraphSuccess", END)
                else:
                    builder.add_edge("Phase3Boundary", END)
                builder.add_edge("GraphFailure", END)
            else:
                builder.add_edge("MergeEvidence", END)
        else:
            builder.add_edge("Plan", END)
        compiled = builder.compile()
        try:
            final = compiled.invoke(state, config={"recursion_limit": 100})
        except LeaseLostError:
            raise
        except Exception:
            # Core node exhaustion and deterministic delivery failures become an explicit Graph
            # result. Runtime still owns the ResearchTask terminal status after this commit.
            delivery_failure["reason"] = failed_node_reason()
            final = graph_failure(coerce(latest_committed()))
        return GraphRunResult(
            outcome=terminal["outcome"],
            state=coerce(final),
            checkpoint_id=cursor["checkpoint_id"],
            failure_reason=terminal.get("failure_reason"),
        )
