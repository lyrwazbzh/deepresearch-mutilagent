from __future__ import annotations

import json
import sys
import tempfile
import time
from dataclasses import asdict, dataclass, replace
from datetime import timedelta
from pathlib import Path
from typing import Literal

from src.agent.subagents.implementations import ChiefArchitect, CriticMaster, DeepScout, RouteNode
from src.agent.bootstrap import seed_configured_models
from src.agent.config import MissingRealProviderSecretError, Settings
from src.agent.container import build_container
from src.agent.schema.capability import CapabilitySnapshot
from src.agent.schema.model_config import ModelBindingSnapshot
from src.agent.schema.task import TaskBudget
from src.agent.schema.runtime import ContextPacket, ExecutionContext
from src.agent.schema.state import ResearchState, RouteDecision
from src.agent.backends.events import InMemoryEventBroker
from src.agent.llm.runtime import ModelCallError, ModelResolver, ModelRuntime
from src.agent.backends.persistence.database import create_session_factory
from src.agent.backends.persistence.models import Base, ResearchTaskRow, utcnow
from src.agent.runtime.agent_loop import AgentLoop
from src.agent.runtime.context import ContextBuilder
from src.agent.runtime.tool_runtime import ToolPolicy, ToolRuntime
from src.agent.task_service import CreateTaskRequest
from src.agent.backends.workspace import InMemoryObjectStore


@dataclass(frozen=True)
class AgentSmokeResult:
    name: str
    status: Literal["PASS", "FAIL"]
    latency_ms: int
    error_code: str | None = None


class _NeverCancelled:
    def is_cancelled(self) -> bool:
        return False


def _error_code(exc: Exception) -> str:
    if isinstance(exc, MissingRealProviderSecretError):
        return "BLOCKED_MISSING_REAL_SECRET"
    if isinstance(exc, ModelCallError):
        return exc.error_code
    return type(exc).__name__


def _measure(name: str, call) -> tuple[AgentSmokeResult, object | None]:
    started = time.perf_counter()
    try:
        value = call()
        return (
            AgentSmokeResult(
                name=name,
                status="PASS",
                latency_ms=round((time.perf_counter() - started) * 1000),
            ),
            value,
        )
    except Exception as exc:
        return (
            AgentSmokeResult(
                name=name,
                status="FAIL",
                latency_ms=round((time.perf_counter() - started) * 1000),
                error_code=_error_code(exc),
            ),
            None,
        )


def _assert_route(route: RouteNode, model, query: str, expected: str) -> None:
    packet = ContextPacket(
        role="route",
        task_id="agent_smoke",
        query=query,
        deadline=utcnow() + timedelta(minutes=2),
        payload={
            "available_execution_domains": [
                "deep_scout",
                "data_analyst",
                "code_wizard",
            ]
        },
    )
    result = route.run(packet, model)
    if result.patch.values["route"]["mode"] != expected:
        raise AssertionError(f"expected {expected} route")


def run() -> list[AgentSmokeResult]:
    settings = Settings.from_env()
    settings.require_real_provider_secrets()
    results: list[AgentSmokeResult] = []
    with tempfile.TemporaryDirectory(prefix="deepresearch-agent-smoke-") as temporary:
        database_url = f"sqlite:///{Path(temporary) / 'smoke.db'}"
        smoke_settings = replace(
            settings,
            database_url=database_url,
            minio_endpoint="",
            elasticsearch_url="",
        )
        sessions = create_session_factory(database_url)
        Base.metadata.create_all(sessions.kw["bind"])
        seed_configured_models(smoke_settings, session_factory=sessions)
        with sessions() as session:
            bindings = ModelResolver().resolve_snapshot(
                session,
                smoke_settings.default_tenant_id,
            )
        runtime = ModelRuntime(
            sessions,
            embedding_dimensions=smoke_settings.openai_embedding_dimensions,
        )

        route = RouteNode()
        route_cases = [
            ("Route direct", "Explain what retrieval-augmented generation means.", "direct"),
            ("Route focused", "Find one recent arXiv paper about retrieval agents.", "focused"),
            (
                "Route deep",
                "Research the market, policy, and technical evidence for enterprise AI agents and produce a cited report.",
                "deep",
            ),
        ]
        for name, query, expected in route_cases:
            result, _ = _measure(
                name,
                lambda query=query, expected=expected: _assert_route(
                    route,
                    runtime.build_chat_model(
                        smoke_settings.default_tenant_id, bindings.get("routing")
                    ),
                    query,
                    expected,
                ),
            )
            results.append(result)

        plan_state = ResearchState(
            tenant_id=smoke_settings.default_tenant_id,
            user_id=smoke_settings.default_user_id,
            thread_id="agent_smoke",
            task_id="agent_smoke",
            query="Compare two evidence-backed approaches to enterprise RAG governance.",
            route=RouteDecision(mode="deep"),
        )
        execution = ExecutionContext(
            tenant_id=smoke_settings.default_tenant_id,
            user_id=smoke_settings.default_user_id,
            task_id="agent_smoke",
            thread_id="agent_smoke",
            lease_token="smoke",
            cancellation_token=_NeverCancelled(),
            deadline=utcnow() + timedelta(minutes=2),
        )
        plan_packet = ContextBuilder().build(
            "chief_plan", plan_state, execution, TaskBudget()
        )
        result, value = _measure(
            "Chief Plan",
            lambda: ChiefArchitect().plan(
                plan_packet,
                runtime.build_chat_model(
                    smoke_settings.default_tenant_id, bindings.get("reasoning")
                ),
            ),
        )
        if result.status == "PASS" and "analysis_requests" in value.patch.values:  # type: ignore[union-attr]
            result = replace(result, status="FAIL", error_code="analysis_request_boundary")
        results.append(result)

        critic_packet = ContextPacket(
            role="critic",
            task_id="agent_smoke",
            query="Does the supplied evidence answer the governance question?",
            deadline=utcnow() + timedelta(minutes=2),
            remaining_budget=TaskBudget().remaining_projection(),
            payload={
                "hypotheses": [
                    {
                        "hypothesis_id": "h1",
                        "claim": "Governance requires access control and traceability.",
                        "evidence_needed": ["primary guidance"],
                        "refuted_if": ["guidance rejects those controls"],
                        "priority": 1,
                    }
                ],
                "coverage": {"complete": True, "gaps": [], "stop_reason": None},
                "evidence": [
                    {
                        "evidence_id": "ev_smoke",
                        "hypothesis_id": "h1",
                        "summary": "A primary guide requires access control and audit traces.",
                        "stance": "support",
                    }
                ],
            },
        )
        result, _ = _measure(
            "Critic",
            lambda: CriticMaster().review(
                critic_packet,
                runtime.build_chat_model(
                    smoke_settings.default_tenant_id, bindings.get("reasoning")
                ),
            ),
        )
        results.append(result)

        container = build_container(
            smoke_settings,
            session_factory=sessions,
            model_runtime=runtime,
            event_broker=InMemoryEventBroker(),  # type: ignore[arg-type]
            workspace_store=InMemoryObjectStore(),
            document_store=InMemoryObjectStore(),
        )
        created = container.task_service.create(
            smoke_settings.default_tenant_id,
            smoke_settings.default_user_id,
            "agent-smoke-deep-scout",
            CreateTaskRequest(query="Find one primary source about retrieval agents."),
        )
        with sessions() as session:
            row = session.get(ResearchTaskRow, created.projection.task_id)
            assert row is not None
            capability_snapshot = CapabilitySnapshot.model_validate(
                row.capability_snapshot
            )
            task_bindings = ModelBindingSnapshot.model_validate(
                row.model_binding_snapshot
            )
        deep_execution = ExecutionContext(
            tenant_id=smoke_settings.default_tenant_id,
            user_id=smoke_settings.default_user_id,
            task_id=created.projection.task_id,
            thread_id=created.projection.thread_id,
            lease_token="smoke",
            cancellation_token=_NeverCancelled(),
            deadline=utcnow() + timedelta(minutes=3),
            capability_snapshot=capability_snapshot.model_dump(mode="json"),
        )
        policy = ToolPolicy(
            frozenset(
                {
                    "search_web",
                    "read_webpage",
                    "render_page",
                    "search_documents",
                    "search_chunks",
                    "read_document",
                }
            )
        )
        scout = DeepScout(
            AgentLoop(
                container.tool_registry,
                max_steps=6,
                tool_runtime=ToolRuntime(
                    container.tool_registry,
                    inline_budget=200_000,
                ),
                skill_runtime=container.skill_runtime,
                policy_registry=container.tool_policy_registry,
            ),
            policy,
        )
        packet = ContextPacket(
            role="deep_scout",
            task_id=created.projection.task_id,
            query="Find one primary source about retrieval agents.",
            deadline=deep_execution.deadline,
            remaining_budget=TaskBudget().remaining_projection(),
            skills=[
                item.model_dump(mode="json")
                for item in container.skill_runtime.metadata(
                    "deep_scout", capability_snapshot
                )
            ],
            payload={
                "assignments": [
                    {
                        "hypothesis": {
                            "hypothesis_id": "h1",
                            "claim": "Retrieval agents are an active research topic.",
                            "evidence_needed": ["one primary paper"],
                            "refuted_if": ["no primary work exists"],
                            "priority": 1,
                        },
                        "queries": [
                            {
                                "query_id": "q1",
                                "channel": "web",
                                "text": "retrieval augmented language model agents arXiv",
                            }
                        ],
                        "relevant_evidence_summaries": [],
                    }
                ],
                "resource_scope": {},
                "runtime": {"node_run_id": "agent_smoke"},
            },
        )
        result, adopted = _measure(
            "DeepScout Tool decision",
            lambda: scout.retrieve(
                packet,
                deep_execution,
                runtime.build_chat_model_for(
                    smoke_settings.default_tenant_id,
                    task_bindings.get("tool"),
                    "deep_scout_agent_smoke",
                ),
                "web",
            ),
        )
        if result.status == "PASS" and not adopted:
            result = replace(result, status="FAIL", error_code="no_adopted_result")
        results.append(result)
        sessions.kw["bind"].dispose()
    return results


def main() -> int:
    try:
        results = run()
    except Exception as exc:
        results = [
            AgentSmokeResult(
                name="Agent smoke configuration",
                status="FAIL",
                latency_ms=0,
                error_code=_error_code(exc),
            )
        ]
    print(json.dumps([asdict(item) for item in results], ensure_ascii=False, indent=2))
    return 0 if all(item.status == "PASS" for item in results) else 1


if __name__ == "__main__":
    sys.exit(main())
