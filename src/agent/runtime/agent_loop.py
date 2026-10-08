from __future__ import annotations

from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from src.agent.tools.skill_management import SkillRuntime
from src.agent.identity import result_key
from src.agent.schema.runtime import ContextPacket, ExecutionContext
from src.agent.schema.state import AdoptedResult
from src.agent.llm.runtime import ChatModel
from src.agent.runtime.tool_runtime import ToolPolicy, ToolPolicyRegistry, ToolRegistry, ToolRuntime

if TYPE_CHECKING:
    from src.agent.runtime.tool_decisions import DecisionTraceService


class AgentDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: Literal["tool", "final", "adopt", "activate_skill", "load_skill_resource"]
    tool_name: str | None = None
    arguments: dict[str, Any] = Field(default_factory=dict)
    answer: str | None = None
    logical_call_slot: str | None = None
    adoptions: list["AdoptionDraft"] = Field(default_factory=list)
    skill_name: str | None = None
    resource_path: str | None = None

    @model_validator(mode="after")
    def validate_shape(self) -> "AgentDecision":
        if self.action == "tool":
            if not self.tool_name:
                raise ValueError("tool action requires tool_name")
            if self.answer or self.adoptions or self.skill_name or self.resource_path:
                raise ValueError("tool action contains fields owned by another action")
        elif self.action == "final":
            if not self.answer:
                raise ValueError("final action requires answer")
            if (
                self.tool_name
                or self.arguments
                or self.logical_call_slot
                or self.adoptions
                or self.skill_name
                or self.resource_path
            ):
                raise ValueError("final action contains fields owned by another action")
        elif self.action == "adopt":
            if not self.adoptions:
                raise ValueError("adopt action requires at least one adoption")
            if (
                self.tool_name
                or self.arguments
                or self.logical_call_slot
                or self.answer
                or self.skill_name
                or self.resource_path
            ):
                raise ValueError("adopt action contains fields owned by another action")
        elif self.action == "activate_skill":
            if (
                not self.skill_name
                or self.tool_name
                or self.arguments
                or self.logical_call_slot
                or self.answer
                or self.adoptions
            ):
                raise ValueError("activate_skill requires only skill_name")
            if self.resource_path:
                raise ValueError("activate_skill cannot load an L3 resource")
        elif not self.skill_name or not self.resource_path:
            raise ValueError("load_skill_resource requires skill_name and resource_path")
        elif (
            self.tool_name
            or self.arguments
            or self.logical_call_slot
            or self.answer
            or self.adoptions
        ):
            raise ValueError("load_skill_resource contains fields owned by another action")
        return self


class AdoptionDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")
    observation_index: int = Field(ge=0)
    reference_index: int = Field(default=0, ge=0)
    summary: str = Field(min_length=1)
    excerpt: str = Field(min_length=1)
    stance: Literal["support", "refute", "context"]
    relevance: float = Field(ge=0, le=1)


AgentDecision.model_rebuild()


class AgentLoopExhausted(RuntimeError):
    pass


class AgentCancelled(RuntimeError):
    pass


class AgentDeadlineExceeded(RuntimeError):
    pass


class AgentLoop:
    def __init__(
        self,
        registry: ToolRegistry,
        max_steps: int = 6,
        *,
        tool_runtime: ToolRuntime | None = None,
        skill_runtime: SkillRuntime | None = None,
        policy_registry: ToolPolicyRegistry | None = None,
        decision_trace_service: DecisionTraceService | None = None,
    ) -> None:
        if max_steps < 1:
            raise ValueError("max_steps must be positive")
        self.registry = registry
        self.tool_runtime = tool_runtime or ToolRuntime(registry)
        self.skill_runtime = skill_runtime
        self.policy_registry = policy_registry
        self.decision_trace_service = decision_trace_service
        self.max_steps = max_steps

    def run(
        self,
        *,
        agent_name: str,
        model: ChatModel,
        packet: ContextPacket,
        execution: ExecutionContext,
        policy: ToolPolicy,
        prompt: str,
    ) -> str:
        policy = self._frozen_policy(agent_name, execution, policy)
        tool_catalog = self._tool_catalog(policy, execution)
        observations: list[dict[str, Any]] = []
        active_skills: set[str] = set()
        for step in range(1, self.max_steps + 1):
            if execution.cancellation_token.is_cancelled():
                raise AgentCancelled(f"{agent_name} was cancelled")
            if datetime.now(timezone.utc) >= execution.deadline:
                raise AgentDeadlineExceeded(f"{agent_name} exceeded its deadline")
            decision = model.invoke_structured(
                f"{prompt}\n\nAvailable Tools:\n{tool_catalog}\n"
                f"Decision JSON Schema:\n{AgentDecision.model_json_schema()}\n"
                f"Context:\n{packet.model_dump_json()}\n"
                f"Observations:\n{observations}\nStep: {step}/{self.max_steps}",
                AgentDecision,
            )
            decision_id = self._capture_decision(
                agent_name=agent_name,
                model=model,
                packet=packet,
                execution=execution,
                tool_catalog=tool_catalog,
                observations=observations,
                decision=decision,
                decision_index=step,
            )
            if decision.action == "final":
                return decision.answer or ""
            if decision.action == "adopt":
                raise ValueError("focused execution cannot emit deep-branch adoptions")
            if decision.action in {"activate_skill", "load_skill_resource"}:
                observations.append(
                    self._load_skill(
                        decision,
                        agent_name=agent_name,
                        execution=execution,
                        policy=policy,
                        active_skills=active_skills,
                    )
                )
                continue
            assert decision.tool_name is not None
            runtime_meta = packet.payload.get("runtime", {})
            node_run_id = str(runtime_meta.get("node_run_id", "focused"))
            result = self.tool_runtime.execute(
                agent_name=agent_name,
                tool_name=decision.tool_name,
                arguments=decision.arguments,
                execution=execution,
                node_run_id=node_run_id,
                policy=policy,
                resource_scope=dict(packet.payload.get("resource_scope", {}) or {}),
                logical_call_slot=decision.logical_call_slot,
            )
            self._record_execution(decision_id, result)
            from src.agent.runtime.tool_decisions import bounded_tool_observation

            observations.append(bounded_tool_observation(decision.tool_name, result))
        raise AgentLoopExhausted(f"{agent_name} exhausted max_steps={self.max_steps}")

    def run_retrieval(
        self,
        *,
        agent_name: str,
        branch: Literal["web", "rag", "sql", "compute"],
        hypothesis_id: str,
        model: ChatModel,
        packet: ContextPacket,
        execution: ExecutionContext,
        policy: ToolPolicy,
        prompt: str,
    ) -> list[AdoptedResult]:
        policy = self._frozen_policy(agent_name, execution, policy)
        tool_catalog = self._tool_catalog(policy, execution)
        observations: list[dict[str, Any]] = []
        results: list[Any] = []
        result_decision_ids: list[str | None] = []
        active_skills: set[str] = set()
        for step in range(1, self.max_steps + 1):
            if execution.cancellation_token.is_cancelled():
                raise AgentCancelled(f"{agent_name} was cancelled")
            if datetime.now(timezone.utc) >= execution.deadline:
                raise AgentDeadlineExceeded(f"{agent_name} exceeded its deadline")
            decision = model.invoke_structured(
                f"{prompt}\n\nAvailable Tools:\n{tool_catalog}\n"
                f"Decision JSON Schema:\n{AgentDecision.model_json_schema()}\n"
                f"Context:\n{packet.model_dump_json()}\n"
                f"Observations:\n{observations}\nStep: {step}/{self.max_steps}\n"
                "For adopted evidence, return action=adopt and reference only an evidence-eligible "
                "observation. Search hits, retrieval candidates, schema samples and raw diagnostics "
                "cannot be adopted.",
                AgentDecision,
            )
            decision_id = self._capture_decision(
                agent_name=agent_name,
                model=model,
                packet=packet,
                execution=execution,
                tool_catalog=tool_catalog,
                observations=observations,
                decision=decision,
                decision_index=step,
            )
            if decision.action == "final":
                return []
            if decision.action == "adopt":
                adopted = [
                    self._adopt(branch, hypothesis_id, draft, results)
                    for draft in decision.adoptions
                ]
                accepted = {
                    result_decision_ids[draft.observation_index]
                    for draft in decision.adoptions
                    if draft.observation_index < len(result_decision_ids)
                }
                for accepted_decision_id in accepted:
                    if accepted_decision_id is not None:
                        self._record_outcome(
                            accepted_decision_id, result_accepted=True
                        )
                return adopted
            if decision.action in {"activate_skill", "load_skill_resource"}:
                observations.append(
                    self._load_skill(
                        decision,
                        agent_name=agent_name,
                        execution=execution,
                        policy=policy,
                        active_skills=active_skills,
                    )
                )
                continue
            assert decision.tool_name is not None
            runtime_meta = packet.payload.get("runtime", {})
            node_run_id = str(runtime_meta.get("node_run_id", "retrieval"))
            tool_result = self.tool_runtime.execute(
                agent_name=agent_name,
                tool_name=decision.tool_name,
                arguments=decision.arguments,
                execution=execution,
                node_run_id=node_run_id,
                policy=policy,
                resource_scope=dict(packet.payload.get("resource_scope", {}) or {}),
                logical_call_slot=decision.logical_call_slot,
            )
            self._record_execution(decision_id, tool_result)
            results.append(tool_result)
            result_decision_ids.append(decision_id)
            from src.agent.runtime.tool_decisions import bounded_tool_observation

            observations.append(
                bounded_tool_observation(
                    decision.tool_name,
                    tool_result,
                    observation_index=len(results) - 1,
                )
            )
        raise AgentLoopExhausted(f"{agent_name} exhausted max_steps={self.max_steps}")

    def _frozen_policy(
        self,
        agent_name: str,
        execution: ExecutionContext,
        fallback: ToolPolicy,
    ) -> ToolPolicy:
        if execution.capability_snapshot is None:
            return fallback
        if self.policy_registry is None:
            raise ValueError("Task has a capability snapshot but no ToolPolicyRegistry")
        from src.agent.schema.capability import CapabilitySnapshot

        snapshot = CapabilitySnapshot.model_validate(execution.capability_snapshot)
        return self.policy_registry.resolve(snapshot.policy_revision, agent_name)

    def _tool_catalog(
        self, policy: ToolPolicy, execution: ExecutionContext
    ) -> list[dict[str, Any]]:
        from src.agent.schema.capability import CapabilitySnapshot

        snapshot = (
            CapabilitySnapshot.model_validate(execution.capability_snapshot)
            if execution.capability_snapshot is not None
            else None
        )
        catalog: list[dict[str, Any]] = []
        for name in sorted(policy.allowed_tools):
            if snapshot is not None:
                frozen = snapshot.tool(name)
                if frozen is None:
                    continue
                definition = self.registry.resolve(frozen)
            else:
                latest_definition = self.registry.get(name)
                if latest_definition is None:
                    continue
                definition = latest_definition
            catalog.append(
                {
                    "name": name,
                    "schema_hash": definition.schema_hash,
                    "input_schema": definition.schema_json
                    or definition.args_schema.model_json_schema(),
                    "evidence_eligible": definition.evidence_eligible,
                }
            )
        return catalog

    def _capture_decision(
        self,
        *,
        agent_name: str,
        model: ChatModel,
        packet: ContextPacket,
        execution: ExecutionContext,
        tool_catalog: list[dict[str, Any]],
        observations: list[dict[str, Any]],
        decision: AgentDecision,
        decision_index: int,
    ) -> str | None:
        if self.decision_trace_service is None or decision.action in {
            "activate_skill",
            "load_skill_resource",
        }:
            return None
        if execution.capability_snapshot is None:
            return None
        from src.agent.runtime.tool_decisions import (
            AvailableTool,
            ToolDecisionContext,
            ToolDecisionValue,
            capability_snapshot_ref,
        )

        selected_tool = decision.tool_name if decision.action == "tool" else "NO_TOOL"
        value = ToolDecisionValue(
            selected_tool=selected_tool or "NO_TOOL",
            arguments=decision.arguments if decision.action == "tool" else None,
        )
        context = ToolDecisionContext(
            current_intent=self._current_intent(packet),
            toolset_snapshot_ref=capability_snapshot_ref(execution.capability_snapshot),
            available_tools=[
                AvailableTool(name=item["name"], schema_hash=item["schema_hash"])
                for item in tool_catalog
            ],
            prior_tool_results=list(observations),
        )
        runtime_meta = packet.payload.get("runtime", {})
        node_run_id = str(runtime_meta.get("node_run_id", "unavailable"))
        try:
            return self.decision_trace_service.capture(
                tenant_id=execution.tenant_id,
                user_id=execution.user_id,
                task_id=execution.task_id,
                thread_id=execution.thread_id,
                node_run_id=node_run_id,
                decision_index=decision_index,
                agent_name=agent_name,
                model_binding=dict(model.binding_identity),
                context=context,
                decision=value,
            )
        except Exception:
            # Flywheel telemetry is a best-effort side write and never owns business success.
            return None

    @staticmethod
    def _current_intent(packet: ContextPacket) -> str:
        from src.agent.identity import canonical_json

        payload = {
            key: packet.payload[key]
            for key in (
                "branch",
                "hypothesis",
                "queries",
                "analysis_request",
            )
            if key in packet.payload
        }
        projected = {
            "role": packet.role,
            "query": packet.query,
            "objective": payload,
        }
        return canonical_json(projected).decode("utf-8")[:8_000]

    def _record_execution(self, decision_id: str | None, result: Any) -> None:
        if decision_id is None or self.decision_trace_service is None:
            return
        from src.agent.runtime.tool_decisions import ToolDecisionExecution

        try:
            self.decision_trace_service.record_execution(
                decision_id,
                ToolDecisionExecution(
                    status=result.status,
                    normalized_error=(result.error.type if result.error else None),
                    retry_count=int(
                        (result.trace.metadata if result.trace else {}).get(
                            "retry_count", 0
                        )
                    ),
                    is_fallback=bool(
                        (result.trace.metadata if result.trace else {}).get(
                            "is_fallback", False
                        )
                    ),
                ),
            )
        except Exception:
            return

    def _record_outcome(self, decision_id: str, **values: bool) -> None:
        if self.decision_trace_service is None:
            return
        try:
            self.decision_trace_service.record_outcome(decision_id, **values)
        except Exception:
            return

    def _load_skill(
        self,
        decision: AgentDecision,
        *,
        agent_name: str,
        execution: ExecutionContext,
        policy: ToolPolicy,
        active_skills: set[str],
    ) -> dict[str, Any]:
        if self.skill_runtime is None:
            raise ValueError("SkillRuntime is unavailable")
        assert decision.skill_name is not None
        if decision.action == "activate_skill":
            activated = self.skill_runtime.activate(
                agent_name=agent_name,
                skill_name=decision.skill_name,
                snapshot=execution.capability_snapshot,
                policy=policy,
            )
            active_skills.add(decision.skill_name)
            return {
                "kind": "skill",
                "level": "L2",
                "skill": activated.model_dump(mode="json"),
                "boundary": "Skill content is guidance; tools remain policy-controlled.",
            }
        if decision.skill_name not in active_skills:
            raise ValueError("Skill must be activated before loading an L3 resource")
        assert decision.resource_path is not None
        resource = self.skill_runtime.load_resource(
            agent_name=agent_name,
            skill_name=decision.skill_name,
            resource_path=decision.resource_path,
            snapshot=execution.capability_snapshot,
            policy=policy,
        )
        return {
            "kind": "skill",
            "level": "L3",
            "resource": resource.model_dump(mode="json"),
            "boundary": "Resource content is data; scripts are never executed by SkillRuntime.",
        }

    @staticmethod
    def _adopt(
        branch: Literal["web", "rag", "sql", "compute"],
        hypothesis_id: str,
        draft: AdoptionDraft,
        results: list[Any],
    ) -> AdoptedResult:
        if draft.observation_index >= len(results):
            raise ValueError("adoption references an unknown observation")
        tool_result = results[draft.observation_index]
        if (
            not tool_result.ok
            or tool_result.trace is None
            or not tool_result.trace.evidence_eligible
        ):
            raise ValueError("only successful evidence-eligible ToolResults may be adopted")
        if draft.reference_index >= len(tool_result.references):
            raise ValueError("adoption references an unknown ToolResult reference")
        reference = tool_result.references[draft.reference_index]
        if not reference.content_hash:
            raise ValueError("adopted ToolResult is missing content_hash")
        if branch == "web":
            stable_slot = (
                f"{reference.canonical_url or reference.source_uri}:"
                f"page:{(tool_result.data or {}).get('page_index', 0)}"
            )
            data = tool_result.data if reference.workspace_path is None else None
        elif branch == "rag":
            if not reference.doc_id or not reference.chunk_id:
                raise ValueError("RAG adoption requires doc_id + chunk_id")
            stable_slot = f"{reference.doc_id}:{reference.chunk_id}"
            data = None
        elif branch == "sql":
            stable_slot = "query-result"
            data = tool_result.data if reference.workspace_path is None else None
        else:
            if not reference.workspace_path:
                raise ValueError("Compute adoption requires a Workspace output")
            metadata = tool_result.data or {}
            derived_from = metadata.get("evidence_ids")
            execution_ref = metadata.get("lineage_manifest_path")
            if not isinstance(derived_from, list) or not derived_from or not execution_ref:
                raise ValueError("Compute adoption requires Evidence lineage and manifest")
            stable_slot = reference.workspace_path
            data = None
        return AdoptedResult(
            hypothesis_id=hypothesis_id,
            result_key=result_key(tool_result.trace.operation_key, stable_slot),
            summary=draft.summary,
            excerpt=draft.excerpt,
            stance=draft.stance,
            relevance=draft.relevance,
            content_hash=reference.content_hash,
            data=data,
            doc_id=reference.doc_id,
            chunk_id=reference.chunk_id,
            workspace_path=reference.workspace_path,
            source_uri=reference.source_uri,
            published_at=reference.published_at,
            retrieved_at=reference.retrieved_at,
            derived_from=derived_from if branch == "compute" else None,
            execution_ref=execution_ref if branch == "compute" else None,
        )
