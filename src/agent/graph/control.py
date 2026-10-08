from __future__ import annotations

from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from src.agent.subagents.implementations import PromptRenderer
from src.agent.identity import analysis_request_id
from src.agent.schema.task import TaskBudget
from src.agent.schema.runtime import ContextPacket, NodeResult, StatePatch
from src.agent.schema.state import (
    AnalysisRequest,
    CoverageGap,
    CoverageResult,
    DeepenRecord,
    Hypothesis,
    HypothesisPlan,
    ResearchQuery,
    ResearchState,
)
from src.agent.llm.runtime import ChatModel


class AnalysisRequestDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")
    hypothesis_id: str = Field(min_length=1)
    objective: str = Field(min_length=1)
    input_evidence_ids: list[str] = Field(min_length=1)
    expected_output: str = Field(min_length=1)


class AnalysisDecisionOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    requests: list[AnalysisRequestDraft] = Field(default_factory=list)


class QueryAddition(BaseModel):
    model_config = ConfigDict(extra="forbid")
    hypothesis_id: str = Field(min_length=1)
    evidence_needed: list[str] = Field(default_factory=list)
    queries: list[ResearchQuery] = Field(min_length=1)


class DeepenProposal(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: str = Field(min_length=1)
    new_hypotheses: list[Hypothesis] = Field(default_factory=list)
    query_additions: list[QueryAddition] = Field(default_factory=list)


class AnalysisDecision:
    """Structured post-merge control step; deliberately not an Agent or Tool loop."""

    def __init__(self, renderer: PromptRenderer | None = None) -> None:
        self.renderer = renderer or PromptRenderer()

    def run(
        self,
        packet: ContextPacket,
        model: ChatModel,
        state: ResearchState,
    ) -> NodeResult:
        if state.plan is None:
            raise ValueError("AnalysisDecision requires a plan")
        decision = model.invoke_structured(
            self.renderer.render("analysis_decision.jinja2", packet),
            AnalysisDecisionOutput,
        )
        hypothesis_ids = {item.hypothesis_id for item in state.plan.hypotheses}
        evidence_ids = {item.evidence_id for item in state.evidence_pool}
        existing = {item.analysis_id: item for item in state.analysis_requests}
        additions: list[dict[str, Any]] = []
        seen: set[str] = set()
        for draft in decision.requests:
            if draft.hypothesis_id not in hypothesis_ids:
                raise ValueError(f"Unknown hypothesis_id: {draft.hypothesis_id}")
            if not set(draft.input_evidence_ids) <= evidence_ids:
                raise ValueError("AnalysisDecision references unknown Evidence")
            identity = analysis_request_id(
                state.task_id,
                draft.hypothesis_id,
                draft.objective,
                draft.input_evidence_ids,
                draft.expected_output,
            )
            request = AnalysisRequest(
                analysis_id=identity,
                hypothesis_id=draft.hypothesis_id,
                objective=draft.objective,
                input_evidence_ids=sorted(set(draft.input_evidence_ids)),
                expected_output=draft.expected_output,
            )
            prior = existing.get(identity)
            if prior is not None:
                if prior.model_dump(exclude={"status"}) != request.model_dump(
                    exclude={"status"}
                ):
                    raise ValueError(f"AnalysisRequest {identity} conflicts with prior state")
                continue
            if identity not in seen:
                additions.append(request.model_dump(mode="json"))
                seen.add(identity)
        return NodeResult(
            patch=StatePatch(analysis_request_appends=additions),
            output={"created_analysis_ids": [item["analysis_id"] for item in additions]},
        )


class CoverageEvaluator:
    """Deterministic structural coverage; it never scores evidence quality."""

    def evaluate(
        self,
        state: ResearchState,
        budget: TaskBudget,
        *,
        no_new_queries: bool = False,
    ) -> CoverageResult:
        if state.plan is None:
            raise ValueError("Coverage requires a plan")
        query_depth = {
            query.query_id: 0
            for hypothesis in state.plan.hypotheses
            for query in hypothesis.queries
        }
        for record in state.plan.deepen_history:
            for query_id in record.added_query_ids:
                query_depth[query_id] = record.depth
        evidence_by_hypothesis = {
            hypothesis.hypothesis_id: [
                item
                for item in state.evidence_pool
                if item.hypothesis_id == hypothesis.hypothesis_id
            ]
            for hypothesis in state.plan.hypotheses
        }
        gaps: list[CoverageGap] = []
        seen: set[tuple[str, str, str | None]] = set()

        def add(gap: CoverageGap) -> None:
            key = (gap.hypothesis_id, gap.gap_type, gap.channel)
            if key not in seen:
                gaps.append(gap)
                seen.add(key)

        for hypothesis in state.plan.hypotheses:
            if not evidence_by_hypothesis[hypothesis.hypothesis_id]:
                add(
                    CoverageGap(
                        hypothesis_id=hypothesis.hypothesis_id,
                        gap_type="missing_evidence",
                        description="Hypothesis has no formal Evidence.",
                    )
                )
            requirements = {
                (query.channel, query_depth.get(query.query_id, 0))
                for query in hypothesis.queries
            }
            for channel, depth in sorted(requirements):
                round_ = state.research_rounds.get(str(depth))
                branch = getattr(round_, channel) if round_ is not None else None
                if branch is None or branch.status in {"failed", "skipped"}:
                    add(
                        CoverageGap(
                            hypothesis_id=hypothesis.hypothesis_id,
                            gap_type="missing_channel",
                            channel=channel,
                            description=f"Required {channel} branch did not complete.",
                        )
                    )
                elif branch.status == "pending":
                    add(
                        CoverageGap(
                            hypothesis_id=hypothesis.hypothesis_id,
                            gap_type="pending_query",
                            channel=channel,
                            description=f"Required {channel} query is still pending.",
                        )
                    )
            for request in state.analysis_requests:
                if request.hypothesis_id == hypothesis.hypothesis_id and request.status != "completed":
                    add(
                        CoverageGap(
                            hypothesis_id=hypothesis.hypothesis_id,
                            gap_type="pending_analysis",
                            description=f"AnalysisRequest {request.analysis_id} is {request.status}.",
                        )
                    )

        if not gaps:
            return CoverageResult(complete=True)
        stop_reason: Literal["max_depth", "budget_exhausted", "no_new_queries"] | None = None
        if no_new_queries or any(item.status == "failed" for item in state.analysis_requests):
            stop_reason = "no_new_queries"
        elif state.execution.depth >= state.execution.max_depth:
            stop_reason = "max_depth"
        elif self._budget_exhausted(budget):
            stop_reason = "budget_exhausted"
        return CoverageResult(complete=False, gaps=gaps, stop_reason=stop_reason)

    @staticmethod
    def _budget_exhausted(budget: TaskBudget) -> bool:
        remaining = budget.remaining_projection()
        return (
            int(remaining["wall_time_ms"]) <= 0
            or int(remaining["search_calls"]) <= 0
            or int(remaining["model_tokens"]) <= 0
            or Decimal(str(remaining["cost"])) <= 0
        )


class DeepenController:
    def __init__(self, renderer: PromptRenderer | None = None) -> None:
        self.renderer = renderer or PromptRenderer()

    def run(
        self,
        packet: ContextPacket,
        model: ChatModel,
        state: ResearchState,
    ) -> NodeResult:
        if state.plan is None:
            raise ValueError("Deepen requires a plan")
        if state.execution.depth >= state.execution.max_depth:
            return NodeResult(output={"no_new_queries": True, "reason": "max_depth"})
        prompt_name = "chief_architect_deepen.jinja2"
        proposal = model.invoke_structured(
            self.renderer.render(prompt_name, packet), DeepenProposal
        )
        plan = HypothesisPlan.model_validate(state.plan.model_dump(mode="json"))
        existing_hypothesis_ids = {item.hypothesis_id for item in plan.hypotheses}
        existing_query_ids = {
            query.query_id for hypothesis in plan.hypotheses for query in hypothesis.queries
        }
        semantic_queries = {
            (query.channel, " ".join(query.text.lower().split()))
            for hypothesis in plan.hypotheses
            for query in hypothesis.queries
        }
        added_hypothesis_ids: list[str] = []
        added_query_ids: list[str] = []

        for hypothesis in proposal.new_hypotheses:
            if hypothesis.hypothesis_id in existing_hypothesis_ids:
                raise ValueError(f"Duplicate hypothesis_id: {hypothesis.hypothesis_id}")
            for query in hypothesis.queries:
                semantic = (query.channel, " ".join(query.text.lower().split()))
                if query.query_id in existing_query_ids or semantic in semantic_queries:
                    raise ValueError("Deepen proposal contains a duplicate query")
                existing_query_ids.add(query.query_id)
                semantic_queries.add(semantic)
                added_query_ids.append(query.query_id)
            plan.hypotheses.append(hypothesis)
            existing_hypothesis_ids.add(hypothesis.hypothesis_id)
            added_hypothesis_ids.append(hypothesis.hypothesis_id)

        by_id = {item.hypothesis_id: item for item in plan.hypotheses}
        for addition in proposal.query_additions:
            target_hypothesis = by_id.get(addition.hypothesis_id)
            if target_hypothesis is None:
                raise ValueError(f"Unknown hypothesis_id: {addition.hypothesis_id}")
            queries = list(target_hypothesis.queries)
            for query in addition.queries:
                semantic = (query.channel, " ".join(query.text.lower().split()))
                if query.query_id in existing_query_ids or semantic in semantic_queries:
                    continue
                existing_query_ids.add(query.query_id)
                semantic_queries.add(semantic)
                queries.append(query)
                added_query_ids.append(query.query_id)
            needed = list(target_hypothesis.evidence_needed)
            for item in addition.evidence_needed:
                if item not in needed:
                    needed.append(item)
            by_id[addition.hypothesis_id] = target_hypothesis.model_copy(
                update={"queries": queries, "evidence_needed": needed}
            )
        plan.hypotheses = [by_id[item.hypothesis_id] for item in plan.hypotheses]

        if not added_hypothesis_ids and not added_query_ids:
            return NodeResult(
                output={
                    "no_new_queries": True,
                    "reason": proposal.reason,
                    "agent_trace": self.renderer.trace(prompt_name, packet, model),
                }
            )
        next_depth = state.execution.depth + 1
        plan.deepen_history.append(
            DeepenRecord(
                depth=next_depth,
                reason=proposal.reason,
                added_hypothesis_ids=added_hypothesis_ids,
                added_query_ids=added_query_ids,
            )
        )
        execution = state.execution.model_copy(update={"depth": next_depth})
        return NodeResult(
            patch=StatePatch(
                values={
                    "plan": plan.model_dump(mode="json"),
                    "execution": execution.model_dump(mode="json"),
                }
            ),
            output={
                "no_new_queries": False,
                "depth": next_depth,
                "added_hypothesis_ids": added_hypothesis_ids,
                "added_query_ids": added_query_ids,
                "agent_trace": self.renderer.trace(prompt_name, packet, model),
            },
        )
