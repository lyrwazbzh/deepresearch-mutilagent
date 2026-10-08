from __future__ import annotations

import hashlib
import json
from datetime import timezone
from typing import Any, Callable, Literal

from src.agent.tools.skill_management import SkillRuntime
from src.agent.schema.task import TaskBudget
from src.agent.schema.runtime import ContextPacket, ExecutionContext
from src.agent.schema.state import MemoryContext, ResearchState


class ContextBuilder:
    def __init__(
        self,
        *,
        available_execution_domains: list[str] | None = None,
        available_sources: list[dict[str, str]] | None = None,
        dataset_scope_resolver: Callable[[str, str], list[dict[str, object]]] | None = None,
        datasource_scope_resolver: Callable[
            [str, str, str, str | None], list[dict[str, Any]]
        ]
        | None = None,
        memory_context_resolver: Callable[
            [str, str, MemoryContext | None], list[dict[str, Any]]
        ]
        | None = None,
        skill_runtime: SkillRuntime | None = None,
    ) -> None:
        self.available_execution_domains = available_execution_domains or [
            "deep_scout",
            "data_analyst",
            "code_wizard",
        ]
        self.available_sources = available_sources or []
        self.dataset_scope_resolver = dataset_scope_resolver or (lambda _tenant, _task: [])
        self.datasource_scope_resolver = datasource_scope_resolver or (
            lambda _tenant, _user, _task, _source: []
        )
        self.memory_context_resolver = memory_context_resolver or (
            lambda _tenant, _user, _context: []
        )
        self.skill_runtime = skill_runtime

    def build(
        self,
        role: Literal[
            "route",
            "chief_direct",
            "chief_plan",
            "chief_replan",
            "deep_scout",
            "data_analyst",
            "code_wizard",
            "deep_scout_web",
            "deep_scout_rag",
            "data_analyst_sql",
            "analysis_decision",
            "code_wizard_compute",
            "coverage",
            "chief_deepen",
            "critic",
            "phase3_boundary",
            "database_write_gate",
            "prepare_write",
            "lead_writer",
            "validate_report",
            "revise",
            "finalize",
            "graph_success",
            "graph_failure",
        ],
        state: ResearchState,
        runtime: ExecutionContext,
        budget: TaskBudget,
    ) -> ContextPacket:
        skill_agent = {
            "chief_direct": "chief_architect",
            "chief_plan": "chief_architect",
            "chief_replan": "chief_architect",
            "chief_deepen": "chief_architect",
            "deep_scout": "deep_scout",
            "deep_scout_web": "deep_scout",
            "deep_scout_rag": "deep_scout",
            "data_analyst": "data_analyst",
            "data_analyst_sql": "data_analyst",
            "prepare_write": "data_analyst",
            "code_wizard": "code_wizard",
            "code_wizard_compute": "code_wizard",
            "critic": "critic_master",
            "lead_writer": "lead_writer",
            "revise": "lead_writer",
        }.get(role)
        skills = (
            self.skill_runtime.metadata(skill_agent, runtime.capability_snapshot)
            if self.skill_runtime is not None and skill_agent is not None
            else []
        )
        base: dict[str, Any] = {
            "role": role,
            "task_id": runtime.task_id,
            "query": state.query,
            "deadline": runtime.deadline.astimezone(timezone.utc),
            "skills": [item.model_dump(mode="json") for item in skills],
        }
        if role == "route":
            return ContextPacket(
                **base,
                payload={"available_execution_domains": self.available_execution_domains},
            )
        if role == "chief_direct":
            return ContextPacket(**base)
        if role == "chief_plan":
            return ContextPacket(
                **base,
                remaining_budget=budget.remaining_projection(),
                payload={
                    "memory_context": state.memory_context.model_dump(mode="json")
                    if state.memory_context
                    else None,
                    "research_memory_prior": {
                        "boundary": (
                            "User research collaboration prior only; not external factual "
                            "evidence and never citable."
                        ),
                        "items": self.memory_context_resolver(
                            runtime.tenant_id, runtime.user_id, state.memory_context
                        ),
                    },
                    "available_sources": self.available_sources,
                },
            )

        if role == "chief_replan":
            return ContextPacket(
                **base,
                remaining_budget=budget.remaining_projection(),
                payload={
                    "previous_plan": state.plan.model_dump(mode="json")
                    if state.plan
                    else None,
                    "memory_context": state.memory_context.model_dump(mode="json")
                    if state.memory_context
                    else None,
                    "research_memory_prior": {
                        "boundary": (
                            "Planning prior only; never treat it as external factual evidence."
                        ),
                        "items": self.memory_context_resolver(
                            runtime.tenant_id, runtime.user_id, state.memory_context
                        ),
                    },
                },
            )

        if role == "analysis_decision":
            return ContextPacket(
                **base,
                remaining_budget=budget.remaining_projection(),
                payload={
                    "hypotheses": [
                        item.model_dump(mode="json") for item in state.plan.hypotheses
                    ]
                    if state.plan
                    else [],
                    "evidence": [item.model_dump(mode="json") for item in state.evidence_pool],
                    "datasets": self.dataset_scope_resolver(runtime.tenant_id, runtime.task_id),
                    "analysis_requests": [
                        item.model_dump(mode="json") for item in state.analysis_requests
                    ],
                },
            )

        if role == "code_wizard_compute":
            request = next(
                (item for item in state.analysis_requests if item.status == "pending"), None
            )
            if request is None:
                raise ValueError("Compute context requires a pending AnalysisRequest")
            evidence = [
                item
                for item in state.evidence_pool
                if item.evidence_id in request.input_evidence_ids
            ]
            hypothesis_payload: dict[str, Any] | None = None
            if state.plan:
                selected = next(
                    (
                        item
                        for item in state.plan.hypotheses
                        if item.hypothesis_id == request.hypothesis_id
                    ),
                    None,
                )
                if selected:
                    hypothesis_payload = selected.model_dump(mode="json")
            datasets = self.dataset_scope_resolver(runtime.tenant_id, runtime.task_id)
            return ContextPacket(
                **base,
                remaining_budget=budget.remaining_projection(),
                payload={
                    "analysis_request": request.model_dump(mode="json"),
                    "hypothesis": hypothesis_payload,
                    "evidence": self._evidence_cards(
                        evidence, include_data=True, max_chars=24_000
                    ),
                    "datasets": datasets,
                    "resource_scope": {
                        "analysis_id": request.analysis_id,
                        "allowed_evidence_ids": request.input_evidence_ids,
                        "allowed_dataset_refs": [item["dataset_ref"] for item in datasets],
                    },
                },
            )

        if role == "chief_deepen":
            gaps = (
                [item.model_dump(mode="json") for item in state.coverage.gaps]
                if state.coverage and not state.coverage.complete
                else [item.model_dump(mode="json") for item in state.critic.gaps]
                if state.critic and state.critic.decision != "pass"
                else []
            )
            return ContextPacket(
                **base,
                remaining_budget=budget.remaining_projection(),
                payload={
                    "plan": state.plan.model_dump(mode="json") if state.plan else None,
                    "gaps": gaps,
                    "depth": state.execution.depth,
                    "evidence_summaries": [
                        {
                            "evidence_id": item.evidence_id,
                            "hypothesis_id": item.hypothesis_id,
                            "summary": item.summary,
                            "stance": item.stance,
                        }
                        for item in state.evidence_pool
                    ],
                    "memory_context": state.memory_context.model_dump(mode="json")
                    if state.memory_context
                    else None,
                    "research_memory_prior": {
                        "boundary": (
                            "User research collaboration prior only; not external factual "
                            "evidence and never citable."
                        ),
                        "items": self.memory_context_resolver(
                            runtime.tenant_id, runtime.user_id, state.memory_context
                        ),
                    },
                },
            )

        if role == "critic":
            return ContextPacket(
                **base,
                remaining_budget=budget.remaining_projection(),
                payload={
                    "hypotheses": [
                        item.model_dump(mode="json") for item in state.plan.hypotheses
                    ]
                    if state.plan
                    else [],
                    "coverage": state.coverage.model_dump(mode="json")
                    if state.coverage
                    else None,
                    "evidence": self._evidence_cards(
                        state.evidence_pool, include_data=False, max_chars=32_000
                    ),
                },
            )

        if role == "prepare_write":
            datasource_id = (
                state.plan.write_intent.datasource_id
                if state.plan and state.plan.write_intent
                else None
            )
            return ContextPacket(
                **base,
                remaining_budget=budget.remaining_projection(),
                payload={
                    "write_intent": state.plan.write_intent.model_dump(mode="json")
                    if state.plan and state.plan.write_intent
                    else None,
                    "critic": state.critic.model_dump(mode="json") if state.critic else None,
                    "datasources": self.datasource_scope_resolver(
                        runtime.tenant_id,
                        runtime.user_id,
                        runtime.task_id,
                        datasource_id,
                    ),
                    "approved_evidence": [
                        {
                            "evidence_id": item.evidence_id,
                            "hypothesis_id": item.hypothesis_id,
                            "summary": item.summary,
                            "stance": item.stance,
                        }
                        for item in state.evidence_pool
                    ],
                },
            )

        if role in {"lead_writer", "revise"}:
            return ContextPacket(
                **base,
                remaining_budget=budget.remaining_projection(),
                payload={
                    "hypotheses": [
                        item.model_dump(mode="json") for item in state.plan.hypotheses
                    ]
                    if state.plan
                    else [],
                    "approved_evidence": self._evidence_cards(
                        state.evidence_pool, include_data=False, max_chars=48_000
                    ),
                    "write_intent": state.plan.write_intent.model_dump(mode="json")
                    if state.plan and state.plan.write_intent
                    else None,
                    "report_version": max(
                        state.report.draft.version + 1 if state.report.draft else 1,
                        state.report.final.version + 1 if state.report.final else 1,
                    ),
                },
            )

        if role in {
            "coverage",
            "phase3_boundary",
            "database_write_gate",
            "validate_report",
            "finalize",
            "graph_success",
            "graph_failure",
        }:
            return ContextPacket(
                **base,
                remaining_budget=budget.remaining_projection(),
                payload={},
            )

        if role in {"deep_scout_web", "deep_scout_rag", "data_analyst_sql"}:
            channel = {
                "deep_scout_web": "web",
                "deep_scout_rag": "rag",
                "data_analyst_sql": "sql",
            }[role]
            assignments = []
            critic = state.critic
            critic_retrieve = critic is not None and critic.decision == "retrieve"
            if state.plan and critic_retrieve and critic is not None:
                hypotheses = {
                    item.hypothesis_id: item for item in state.plan.hypotheses
                }
                for gap in critic.gaps:
                    if gap.target_channel != channel:
                        continue
                    hypothesis = hypotheses.get(gap.hypothesis_id)
                    if hypothesis is None:
                        raise ValueError(
                            f"Critic references unknown hypothesis: {gap.hypothesis_id}"
                        )
                    digest = hashlib.sha256(
                        f"{gap.hypothesis_id}:{channel}:{gap.reason}".encode("utf-8")
                    ).hexdigest()[:24]
                    assignments.append(
                        {
                            "hypothesis": hypothesis.model_dump(
                                mode="json", exclude={"queries"}
                            ),
                            "queries": [
                                {
                                    "query_id": f"critic_{digest}",
                                    "channel": channel,
                                    "text": gap.reason,
                                }
                            ],
                            "relevant_evidence_summaries": [
                                {
                                    "evidence_id": evidence.evidence_id,
                                    "summary": evidence.summary,
                                    "stance": evidence.stance,
                                }
                                for evidence in state.evidence_pool
                                if evidence.hypothesis_id == hypothesis.hypothesis_id
                            ],
                        }
                    )
            elif state.plan:
                allowed_query_ids: set[str] | None = None
                if state.execution.depth > 0:
                    record = next(
                        (
                            item
                            for item in state.plan.deepen_history
                            if item.depth == state.execution.depth
                        ),
                        None,
                    )
                    allowed_query_ids = set(record.added_query_ids) if record else set()
                for hypothesis in state.plan.hypotheses:
                    queries = [
                        query.model_dump(mode="json")
                        for query in hypothesis.queries
                        if query.channel == channel
                        and (
                            allowed_query_ids is None
                            or query.query_id in allowed_query_ids
                        )
                    ]
                    if queries:
                        assignments.append(
                            {
                                "hypothesis": hypothesis.model_dump(
                                    mode="json", exclude={"queries"}
                                ),
                                "queries": queries,
                                "relevant_evidence_summaries": [
                                    {
                                        "evidence_id": evidence.evidence_id,
                                        "summary": evidence.summary,
                                        "stance": evidence.stance,
                                    }
                                    for evidence in state.evidence_pool
                                    if evidence.hypothesis_id == hypothesis.hypothesis_id
                                ],
                            }
                        )
            return ContextPacket(
                **{
                    **base,
                    "role": "deep_scout"
                    if channel in {"web", "rag"}
                    else "data_analyst",
                },
                remaining_budget=budget.remaining_projection(),
                payload={
                    "branch": channel,
                    "assignments": assignments,
                    "resource_scope": {
                        "datasets": self.dataset_scope_resolver(
                            runtime.tenant_id, runtime.task_id
                        )
                        if channel == "sql"
                        else []
                    },
                },
            )

        focused_payload: dict[str, object] = {"resource_scope": {}}
        if role == "deep_scout":
            focused_payload["hypothesis"] = None
            focused_payload["queries"] = []
            focused_payload["evidence_summaries"] = []
        elif role == "data_analyst":
            focused_payload["hypothesis"] = None
            focused_payload["sql_query"] = None
            focused_payload["datasource_dataset_scope"] = self.dataset_scope_resolver(
                runtime.tenant_id, runtime.task_id
            )
        else:
            focused_payload["analysis_request"] = None
            focused_payload["evidence_dataset_refs"] = []
        return ContextPacket(
            **base,
            remaining_budget=budget.remaining_projection(),
            payload=focused_payload,
        )

    @staticmethod
    def _evidence_cards(
        evidence: list[Any], *, include_data: bool, max_chars: int
    ) -> list[dict[str, Any]]:
        cards: list[dict[str, Any]] = []
        used = 0
        for item in evidence:
            card = {
                "evidence_id": item.evidence_id,
                "hypothesis_id": item.hypothesis_id,
                "summary": item.summary[:1_500],
                "excerpt": item.excerpt[:3_000],
                "source_type": item.source_type,
                "stance": item.stance,
                "relevance": item.relevance,
                "doc_id": item.doc_id,
                "chunk_id": item.chunk_id,
                "workspace_path": item.workspace_path,
                "source_uri": item.source_uri,
                "published_at": item.published_at.isoformat()
                if item.published_at
                else None,
                "retrieved_at": item.retrieved_at.isoformat()
                if item.retrieved_at
                else None,
                "content_hash": item.content_hash,
                "derived_from": item.derived_from,
                "execution_ref": item.execution_ref,
            }
            if include_data and item.data is not None:
                card["data"] = item.data
            encoded = json.dumps(card, ensure_ascii=False, default=str)
            if cards and used + len(encoded) > max_chars:
                break
            if len(encoded) > max_chars:
                card.pop("data", None)
                card["excerpt"] = card["excerpt"][:1_000]
                encoded = json.dumps(card, ensure_ascii=False, default=str)
            cards.append(card)
            used += len(encoded)
        return cards
