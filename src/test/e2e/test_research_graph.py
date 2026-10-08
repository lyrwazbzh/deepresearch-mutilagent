from __future__ import annotations

from collections import Counter, deque
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Any

import pytest
from sqlalchemy import func, select

from src.agent.tools.documents import IngestionService
from src.agent.config import Settings
from src.agent.container import build_container
from src.agent.tools.database_write import CompiledWrite
from src.agent.identity import content_hash, evidence_id, result_key
from src.agent.schema.model_config import FrozenModelBinding, ResolvedModelRevision
from src.agent.schema.write import WriteResult
from src.agent.backends.events import InMemoryEventBroker
from src.agent.llm.runtime import FakeModelRuntime
from src.agent.backends.persistence.models import (
    DataSourceRow,
    DataSourceTablePolicyRow,
    DataSourceUserGrantRow,
    DecisionTraceRow,
    NodeEffectRow,
    NodeRunRow,
    OutboxMessageRow,
    ResearchCheckpointRow,
    ResearchEventRow,
    ResearchTaskRow,
)
from src.agent.runtime.tool_runtime import ToolReference, ToolResult
from src.agent.task_service import CreateTaskRequest


def phase8_settings(*, phase3: bool = True, phase4: bool = True) -> Settings:
    return Settings(
        database_url="sqlite://",
        redis_url="redis://unused",
        celery_broker_url="redis://unused",
        default_tenant_id="tenant_test",
        default_user_id="user_test",
        model_mode="fake",
        phase2_enabled=True,
        phase3_enabled=phase3,
        phase4_enabled=phase4,
    )


def plan(*channels: str, write: bool = False) -> dict[str, Any]:
    return {
        "hypotheses": [
            {
                "hypothesis_id": "h1",
                "claim": "The evidence supports the research conclusion.",
                "evidence_needed": ["A traceable primary result"],
                "refuted_if": ["A traceable contradictory result"],
                "priority": 1,
                "queries": [
                    {
                        "query_id": f"q_{channel}",
                        "channel": channel,
                        "text": f"Find {channel} evidence",
                    }
                    for channel in channels
                ],
            }
        ],
        "write_intent": (
            {
                "intent_id": "intent_1",
                "description": "Mark the approved account active",
                "datasource_id": "ds1",
            }
            if write
            else None
        ),
        "deepen_history": [],
    }


def report(evidence: str, *, cited: bool = True) -> dict[str, Any]:
    return {
        "report_version": 1,
        "title": "Phase 8 deterministic report",
        "sections": [
            {
                "section_id": "summary",
                "heading": "Summary",
                "level": 2,
                "blocks": [
                    {
                        "type": "paragraph",
                        "segments": [
                            {
                                "type": "claim",
                                "claim_id": "claim_1",
                                "text": "The approved evidence supports the conclusion.",
                                "citation_ids": ["cite_1"] if cited else [],
                            }
                        ],
                    }
                ],
            }
        ],
        "citations": (
            [{"citation_id": "cite_1", "evidence_id": evidence}] if cited else []
        ),
    }


@dataclass
class ToolLog:
    calls: Counter[str]
    evidence_ids: list[str]

    @classmethod
    def create(cls) -> "ToolLog":
        return cls(Counter(), [])


class FakeSearch:
    def __init__(self, log: ToolLog) -> None:
        self.log = log

    def execute(self, context, arguments):
        del context, arguments
        self.log.calls["search_web"] += 1
        return ToolResult(
            status="success",
            summary="one deterministic candidate",
            data={
                "hits": [
                    {
                        "title": "Primary source",
                        "url": "https://example.test/source",
                        "canonical_url": "https://example.test/source",
                        "snippet": "candidate only",
                        "source": "web",
                    }
                ]
            },
        )


class FakeRead:
    def __init__(self, log: ToolLog, *, shared_payload: dict[str, Any] | None = None) -> None:
        self.log = log
        self.shared_payload = shared_payload

    def execute(self, context, arguments):
        del arguments
        self.log.calls["read_webpage"] += 1
        data = self.shared_payload or {
            "content": "authoritative evidence",
            "page_index": 0,
            "has_more": False,
        }
        uri = "https://example.test/source"
        key = result_key(context.operation_key, f"{uri}:page:{data.get('page_index', 0)}")
        self.log.evidence_ids.append(evidence_id(context.task_id, "h1", "web", key))
        return ToolResult(
            status="success",
            summary="authoritative evidence",
            data=data,
            references=[
                ToolReference(
                    source_uri=uri,
                    canonical_url=uri,
                    retrieved_at=datetime.now(timezone.utc),
                    content_hash=content_hash(data),
                )
            ],
        )


class FakeSql:
    def __init__(self, log: ToolLog, *, shared_payload: dict[str, Any] | None = None) -> None:
        self.log = log
        self.shared_payload = shared_payload

    def execute(self, context, arguments):
        del arguments
        self.log.calls["execute_readonly_sql"] += 1
        data = self.shared_payload or {
            "columns": ["metric"],
            "rows": [[42]],
            "truncated": False,
        }
        key = result_key(context.operation_key, "query-result")
        self.log.evidence_ids.append(evidence_id(context.task_id, "h1", "sql", key))
        return ToolResult(
            status="success",
            summary="one deterministic SQL row",
            data=data,
            references=[ToolReference(content_hash=content_hash(data))],
        )


class FakeFocusedCompute:
    def __init__(self, log: ToolLog) -> None:
        self.log = log

    def execute(self, context, arguments):
        del context, arguments
        self.log.calls["render_table"] += 1
        return ToolResult(
            status="success",
            summary="deterministic focused compute",
            data={"rendered_rows": 1},
        )


WEB_ACTIONS = [
    {"action": "tool", "tool_name": "search_web", "arguments": {"query": "q"}},
    {
        "action": "tool",
        "tool_name": "read_webpage",
        "arguments": {"url": "https://example.test/source"},
    },
    {
        "action": "adopt",
        "adoptions": [
            {
                "observation_index": 1,
                "summary": "source supports claim",
                "excerpt": "authoritative evidence",
                "stance": "support",
                "relevance": 0.95,
            }
        ],
    },
]

SQL_ACTIONS = [
    {
        "action": "tool",
        "tool_name": "execute_readonly_sql",
        "arguments": {"sql": "SELECT 42 AS metric"},
    },
    {
        "action": "adopt",
        "adoptions": [
            {
                "observation_index": 0,
                "summary": "SQL result supports claim",
                "excerpt": "metric=42",
                "stance": "support",
                "relevance": 0.9,
            }
        ],
    },
]


def report_response(log: ToolLog, *, cited: bool = True):
    def respond(_prompt: str, _schema: object) -> dict[str, Any]:
        assert log.evidence_ids
        return report(log.evidence_ids[0], cited=cited)

    return respond


def analysis_response(log: ToolLog):
    def respond(_prompt: str, _schema: object) -> dict[str, Any]:
        assert log.evidence_ids
        return {
            "requests": [
                {
                    "hypothesis_id": "h1",
                    "objective": "Render the evidence as a compact table.",
                    "input_evidence_ids": [log.evidence_ids[0]],
                    "expected_output": "A deterministic table artifact.",
                }
            ]
        }

    return respond


def compute_decision(log: ToolLog):
    def respond(_prompt: str, _schema: object) -> dict[str, Any]:
        assert log.evidence_ids
        return {
            "action": "tool",
            "tool_name": "render_table",
            "arguments": {
                "inline": {"columns": ["metric", "value"], "rows": [["support", 1]]},
                "evidence_ids": [log.evidence_ids[0]],
                "title": "Evidence summary",
                "format": "markdown",
            },
        }

    return respond


def build_harness(
    session_factory,
    scripted: dict[str, list[object]],
    *,
    phase3: bool = True,
    phase4: bool = True,
    focused_compute: bool = False,
    write_target_factory=None,
    shared_payload: dict[str, Any] | None = None,
):
    log = ToolLog.create()
    runtime = FakeModelRuntime(scripted)
    container = build_container(
        phase8_settings(phase3=phase3, phase4=phase4),
        session_factory=session_factory,
        model_runtime=runtime,
        event_broker=InMemoryEventBroker(),
        write_target_factory=write_target_factory,
    )
    container.tool_registry.replace(
        replace(container.tool_registry.get("search_web"), adapter=FakeSearch(log))
    )
    container.tool_registry.replace(
        replace(
            container.tool_registry.get("read_webpage"),
            adapter=FakeRead(log, shared_payload=shared_payload),
        )
    )
    container.tool_registry.replace(
        replace(
            container.tool_registry.get("execute_readonly_sql"),
            adapter=FakeSql(log, shared_payload=shared_payload),
        )
    )
    if focused_compute:
        container.tool_registry.replace(
            replace(
                container.tool_registry.get("render_table"),
                adapter=FakeFocusedCompute(log),
            )
        )
    return container, runtime, log


def run_task(container, key: str, query: str = "research question"):
    task_id = container.task_service.create(
        "tenant_test", "user_test", key, CreateTaskRequest(query=query)
    ).projection.task_id
    return task_id, container.worker.run(task_id)


def assert_committed_execution(session_factory, task_id: str) -> list[str]:
    with session_factory() as session:
        nodes = list(
            session.scalars(
                select(NodeRunRow.node_name)
                .where(NodeRunRow.task_id == task_id)
                .order_by(NodeRunRow.created_at)
            ).all()
        )
        completed = session.scalar(
            select(func.count()).select_from(NodeRunRow).where(
                NodeRunRow.task_id == task_id,
                NodeRunRow.status == "completed",
            )
        )
        checkpoints = session.scalar(
            select(func.count()).select_from(ResearchCheckpointRow).where(
                ResearchCheckpointRow.task_id == task_id
            )
        )
        events = session.scalar(
            select(func.count()).select_from(ResearchEventRow).where(
                ResearchEventRow.task_id == task_id
            )
        )
        outbox = session.scalar(
            select(func.count()).select_from(OutboxMessageRow).where(
                OutboxMessageRow.aggregate_id == task_id
            )
        )
        assert completed == len(nodes)
        assert checkpoints == len(nodes) + 1
        assert int(events or 0) >= len(nodes) + 1
        assert int(outbox or 0) >= 1
        return nodes


def test_phase8_direct_runs_to_graph_success(session_factory) -> None:
    container, _, log = build_harness(
        session_factory,
        {
            "routing:route": [{"mode": "direct"}],
            "reasoning:chief_direct": ["direct answer"],
        },
    )
    task_id, result = run_task(container, "phase8-direct")
    assert result.outcome == "success"
    assert result.state.plan is None and not result.state.evidence_pool
    assert not log.calls
    with session_factory() as session:
        assert session.get(ResearchTaskRow, task_id).status == "completed"


@pytest.mark.parametrize(
    ("agent", "binding", "actions", "tool_name", "focused_compute"),
    [
        (
            "deep_scout",
            "tool:deep_scout",
            [*WEB_ACTIONS[:2], {"action": "final", "answer": "web answer"}],
            "read_webpage",
            False,
        ),
        (
            "data_analyst",
            "data:data_analyst",
            [SQL_ACTIONS[0], {"action": "final", "answer": "data answer"}],
            "execute_readonly_sql",
            False,
        ),
        (
            "code_wizard",
            "code:code_wizard",
            [
                {
                    "action": "tool",
                    "tool_name": "render_table",
                    "arguments": {
                        "inline": {"columns": ["x"], "rows": [[1]]},
                        "evidence_ids": ["focused-input"],
                    },
                },
                {"action": "final", "answer": "compute answer"},
            ],
            "render_table",
            True,
        ),
    ],
)
def test_phase8_focused_agents_use_toolruntime_and_finish(
    session_factory, agent, binding, actions, tool_name, focused_compute
) -> None:
    container, _, log = build_harness(
        session_factory,
        {
            "routing:route": [{"mode": "focused", "target_agent": agent}],
            binding: actions,
        },
        focused_compute=focused_compute,
    )
    task_id, result = run_task(container, f"phase8-focused-{agent}")
    assert result.outcome == "success"
    assert result.state.plan is None and not result.state.evidence_pool
    assert log.calls[tool_name] == 1
    with session_factory() as session:
        assert session.scalar(
            select(func.count()).select_from(NodeEffectRow).where(
                NodeEffectRow.task_id == task_id
            )
        ) >= 1
        decisions = list(
            session.scalars(
                select(DecisionTraceRow).where(DecisionTraceRow.task_id == task_id)
            )
        )
        assert decisions
        assert {row.agent_name for row in decisions} == {agent}
        assert tool_name in {row.decision["selected_tool"] for row in decisions}
        assert "NO_TOOL" in {row.decision["selected_tool"] for row in decisions}


def deep_script(log: ToolLog) -> dict[str, list[object]]:
    return {
        "routing:route": [{"mode": "deep"}],
        "reasoning:chief_plan": [plan("web")],
        "reasoning:analysis_decision": [{"requests": []}],
        "reasoning:critic": [{"score": 9, "decision": "pass", "gaps": []}],
        "tool:deep_scout_web": WEB_ACTIONS,
        "writing:lead_writer": [report_response(log)],
    }


def test_phase8_deep_no_compute_query_to_final(session_factory) -> None:
    log = ToolLog.create()
    scripted = deep_script(log)
    container, runtime, actual_log = build_harness(session_factory, scripted)
    # The callbacks and adapters intentionally share the same deterministic execution log.
    scripted_log = log
    runtime._scripted["writing:lead_writer"] = deque([report_response(actual_log)])
    task_id, result = run_task(container, "phase8-deep-no-compute")
    assert result.outcome == "success"
    assert result.state.report.final == result.state.report.draft
    assert {item.source_type for item in result.state.evidence_pool} == {"web"}
    assert not result.state.analysis_requests
    assert scripted_log.evidence_ids == []
    nodes = assert_committed_execution(session_factory, task_id)
    assert nodes.index("critic") < nodes.index("lead_writer")
    assert "code_wizard_compute" not in nodes
    assert nodes[-3:] == ["validate_report", "finalize", "graph_success"]


def test_phase8_deep_compute_second_merge_and_derived_evidence(session_factory) -> None:
    container, runtime, log = build_harness(session_factory, {})
    runtime._scripted.update(
        {
            "routing:route": deque([{"mode": "deep"}]),
            "reasoning:chief_plan": deque([plan("web")]),
            "reasoning:analysis_decision": deque([analysis_response(log)]),
            "reasoning:critic": deque(
                [{"score": 9, "decision": "pass", "gaps": []}]
            ),
            "tool:deep_scout_web": deque(WEB_ACTIONS),
            "code:code_wizard_compute": deque(
                [
                    compute_decision(log),
                    {
                        "action": "adopt",
                        "adoptions": [
                            {
                                "observation_index": 0,
                                "summary": "derived table",
                                "excerpt": "support=1",
                                "stance": "support",
                                "relevance": 0.9,
                            }
                        ],
                    },
                ]
            ),
            "writing:lead_writer": deque([report_response(log)]),
        }
    )
    task_id, result = run_task(container, "phase8-deep-compute")
    assert result.outcome == "success"
    assert {item.status for item in result.state.analysis_requests} == {"completed"}
    compute = [item for item in result.state.evidence_pool if item.source_type == "compute"]
    assert len(compute) == 1
    assert compute[0].derived_from == [log.evidence_ids[0]]
    nodes = assert_committed_execution(session_factory, task_id)
    assert nodes.count("merge_evidence") == 2
    assert nodes.index("code_wizard_compute") < nodes.index("coverage")


def test_phase8_coverage_deepen_runs_new_round_before_critic(session_factory) -> None:
    container, runtime, log = build_harness(session_factory, {})
    runtime._scripted.update(
        {
            "routing:route": deque([{"mode": "deep"}]),
            "reasoning:chief_plan": deque([plan("web")]),
            "reasoning:analysis_decision": deque(
                [{"requests": []}, {"requests": []}]
            ),
            "reasoning:chief_deepen": deque(
                [
                    {
                        "reason": "The first round returned no adopted evidence.",
                        "new_hypotheses": [],
                        "query_additions": [
                            {
                                "hypothesis_id": "h1",
                                "evidence_needed": ["A second source"],
                                "queries": [
                                    {
                                        "query_id": "q_web_round_1",
                                        "channel": "web",
                                        "text": "Find a second source",
                                    }
                                ],
                            }
                        ],
                    }
                ]
            ),
            "reasoning:critic": deque(
                [{"score": 9, "decision": "pass", "gaps": []}]
            ),
            "tool:deep_scout_web": deque(
                [{"action": "final", "answer": "no result"}, *WEB_ACTIONS]
            ),
            "writing:lead_writer": deque([report_response(log)]),
        }
    )
    task_id, result = run_task(container, "phase8-coverage-deepen")
    assert result.outcome == "success"
    assert result.state.execution.depth == 1
    assert set(result.state.research_rounds) == {"0", "1"}
    nodes = assert_committed_execution(session_factory, task_id)
    assert nodes.count("coverage") == 2
    assert nodes.index("chief_deepen") < nodes.index("critic")


@pytest.mark.parametrize("decision", ["retrieve", "deepen"])
def test_phase8_critic_gap_reenters_full_loop_and_then_finalizes(
    session_factory, decision
) -> None:
    container, runtime, log = build_harness(session_factory, {})
    critic_gap = {
        "score": 6,
        "decision": decision,
        "gaps": [
            {
                "hypothesis_id": "h1",
                "reason": "Need independent confirmation",
                **({"target_channel": "web"} if decision == "retrieve" else {}),
            }
        ],
    }
    runtime._scripted.update(
        {
            "routing:route": deque([{"mode": "deep"}]),
            "reasoning:chief_plan": deque([plan("web")]),
            "reasoning:analysis_decision": deque(
                [{"requests": []}, {"requests": []}]
            ),
            "reasoning:critic": deque(
                [critic_gap, {"score": 9, "decision": "pass", "gaps": []}]
            ),
            "tool:deep_scout_web": deque([*WEB_ACTIONS, *WEB_ACTIONS]),
            "writing:lead_writer": deque([report_response(log)]),
        }
    )
    if decision == "deepen":
        runtime._scripted["reasoning:chief_deepen"] = deque(
            [
                {
                    "reason": "Add independent confirmation",
                    "new_hypotheses": [],
                    "query_additions": [
                        {
                            "hypothesis_id": "h1",
                            "evidence_needed": ["Independent confirmation"],
                            "queries": [
                                {
                                    "query_id": "q_web_critic_deepen",
                                    "channel": "web",
                                    "text": "Independent confirmation",
                                }
                            ],
                        }
                    ],
                }
            ]
        )
    task_id, result = run_task(container, f"phase8-critic-{decision}")
    assert result.outcome == "success"
    nodes = assert_committed_execution(session_factory, task_id)
    assert nodes.count("critic") == 2
    assert nodes.count("merge_evidence") == 2
    assert nodes.count("analysis_decision") == 2
    assert nodes.count("coverage") == 2
    assert nodes.index("critic") < nodes.index("lead_writer")


@pytest.mark.parametrize("revision_succeeds", [True, False])
def test_phase8_writer_revision_loop_never_returns_to_research(
    session_factory, revision_succeeds
) -> None:
    container, runtime, log = build_harness(session_factory, {})
    revisions: list[object] = [report_response(log, cited=revision_succeeds)]
    if not revision_succeeds:
        revisions.append(report_response(log, cited=False))
    runtime._scripted.update(
        {
            "routing:route": deque([{"mode": "deep"}]),
            "reasoning:chief_plan": deque([plan("web")]),
            "reasoning:analysis_decision": deque([{"requests": []}]),
            "reasoning:critic": deque(
                [{"score": 9, "decision": "pass", "gaps": []}]
            ),
            "tool:deep_scout_web": deque(WEB_ACTIONS),
            "writing:lead_writer": deque([report_response(log, cited=False)]),
            "writing:revise": deque(revisions),
        }
    )
    task_id, result = run_task(
        container, f"phase8-writer-revise-{revision_succeeds}"
    )
    assert result.outcome == ("success" if revision_succeeds else "failure")
    assert (result.state.report.final is not None) is revision_succeeds
    assert result.state.execution.revision_round == (1 if revision_succeeds else 2)
    nodes = assert_committed_execution(session_factory, task_id)
    writer_index = nodes.index("lead_writer")
    assert "critic" not in nodes[writer_index + 1 :]
    assert "coverage" not in nodes[writer_index + 1 :]
    assert "merge_evidence" not in nodes[writer_index + 1 :]


def test_phase8_depth_exhaustion_fails_without_writer_or_final(session_factory) -> None:
    container, runtime, _ = build_harness(session_factory, {})
    runtime._scripted.update(
        {
            "routing:route": deque([{"mode": "deep"}]),
            "reasoning:chief_plan": deque([plan("web")]),
            "reasoning:analysis_decision": deque([{"requests": []}]),
            "tool:deep_scout_web": deque(
                [{"action": "final", "answer": "no adopted evidence"}]
            ),
        }
    )
    task_id = container.task_service.create(
        "tenant_test",
        "user_test",
        "phase8-depth-exhausted",
        CreateTaskRequest(query="research question"),
    ).projection.task_id
    with session_factory() as session, session.begin():
        task = session.get(ResearchTaskRow, task_id)
        state = dict(task.latest_state)
        state["execution"] = {**state["execution"], "max_depth": 0}
        task.latest_state = state
    result = container.worker.run(task_id)
    assert result.outcome == "failure"
    assert result.failure_reason == "max_depth"
    assert result.state.report.final is None
    nodes = assert_committed_execution(session_factory, task_id)
    assert "lead_writer" not in nodes


@pytest.mark.parametrize(
    ("scripted", "reason"),
    [
        ({"routing:route": [{"mode": "invalid"}]}, "route_structured_output_failed"),
        (
            {
                "routing:route": [{"mode": "deep"}],
                "reasoning:chief_plan": [{"hypotheses": [], "deepen_history": []}],
            },
            "plan_structured_output_failed",
        ),
    ],
)
def test_phase8_structured_node_failure_is_graph_failure(
    session_factory, scripted, reason
) -> None:
    container, _, _ = build_harness(session_factory, scripted)
    task_id, result = run_task(container, f"phase8-{reason}")
    assert result.outcome == "failure"
    assert result.failure_reason == reason
    assert result.state.report.final is None
    with session_factory() as session:
        task = session.get(ResearchTaskRow, task_id)
        assert task.status == "failed"
        assert task.latest_state["reliability"]["errors"][-1]["reason"] == reason


def test_phase8_no_new_query_is_controlled_graph_failure(session_factory) -> None:
    container, runtime, _ = build_harness(session_factory, {})
    runtime._scripted.update(
        {
            "routing:route": deque([{"mode": "deep"}]),
            "reasoning:chief_plan": deque([plan("web")]),
            "reasoning:analysis_decision": deque([{"requests": []}]),
            "reasoning:chief_deepen": deque(
                [
                    {
                        "reason": "No non-duplicate query remains.",
                        "new_hypotheses": [],
                        "query_additions": [],
                    }
                ]
            ),
            "tool:deep_scout_web": deque(
                [{"action": "final", "answer": "no adopted evidence"}]
            ),
        }
    )
    task_id, result = run_task(container, "phase8-no-new-query")
    assert result.outcome == "failure"
    assert result.failure_reason == "no_new_queries"
    nodes = assert_committed_execution(session_factory, task_id)
    assert "lead_writer" not in nodes


def test_phase8_report_publication_failure_never_promotes_final(
    session_factory, monkeypatch
) -> None:
    container, runtime, log = build_harness(session_factory, {})
    runtime._scripted.update(
        {
            "routing:route": deque([{"mode": "deep"}]),
            "reasoning:chief_plan": deque([plan("web")]),
            "reasoning:analysis_decision": deque([{"requests": []}]),
            "reasoning:critic": deque(
                [{"score": 9, "decision": "pass", "gaps": []}]
            ),
            "tool:deep_scout_web": deque(WEB_ACTIONS),
            "writing:lead_writer": deque([report_response(log)]),
        }
    )

    def fail_publish(**_kwargs):
        raise OSError("simulated workspace publication outage")

    monkeypatch.setattr(container.graph.report_publisher, "publish", fail_publish)
    task_id, result = run_task(container, "phase8-publication-failure")
    assert result.outcome == "failure"
    assert result.failure_reason == "report_publication_failed"
    assert result.state.report.draft is None
    assert result.state.report.final is None
    with session_factory() as session:
        nodes = list(
            session.scalars(
                select(NodeRunRow.node_name)
                .where(NodeRunRow.task_id == task_id)
                .order_by(NodeRunRow.created_at)
            ).all()
        )
        failed = list(
            session.scalars(
                select(NodeRunRow.node_name).where(
                    NodeRunRow.task_id == task_id,
                    NodeRunRow.status == "failed",
                )
            ).all()
        )
    assert failed == ["validate_report"]
    assert "finalize" not in nodes


def seed_write_policy(session_factory) -> None:
    with session_factory() as session, session.begin():
        session.add(
            DataSourceRow(
                datasource_id="ds1",
                tenant_id="tenant_test",
                name="Business DB",
                host="localhost",
                port=5432,
                database_name="business",
                ssl_mode="require",
                credential_ref="PHASE8_TEST_DB_SECRET",
                enabled=True,
            )
        )
        session.add(
            DataSourceUserGrantRow(
                tenant_id="tenant_test",
                datasource_id="ds1",
                user_id="user_test",
                access_level="write",
            )
        )
        session.add(
            DataSourceTablePolicyRow(
                tenant_id="tenant_test",
                datasource_id="ds1",
                schema_name="public",
                table_name="accounts",
                readable_columns=["id", "status"],
                writable_columns=["id", "status"],
                allow_insert=False,
                allow_update=True,
                allow_delete=False,
                max_write_rows=1,
            )
        )


@dataclass
class FakeWriteTarget:
    calls: list[CompiledWrite]

    def execute(self, **kwargs):
        self.calls.append(kwargs["compiled"])
        return WriteResult(
            status="executed",
            affected_rows=1,
            operation_key=kwargs["operation_key"],
        )


class FakeWriteTargetFactory:
    def __init__(self) -> None:
        self.target = FakeWriteTarget([])

    def create(self, datasource, credential):
        del datasource, credential
        return self.target


def test_phase8_write_intent_pauses_and_resumes_main_chain(
    session_factory, monkeypatch
) -> None:
    seed_write_policy(session_factory)
    monkeypatch.setenv(
        "PHASE8_TEST_DB_SECRET",
        '{"read":{"username":"reader","password":"x"},'
        '"write":{"username":"writer","password":"y"}}',
    )
    factory = FakeWriteTargetFactory()
    container, runtime, log = build_harness(
        session_factory, {}, write_target_factory=factory
    )
    runtime._scripted.update(
        {
            "routing:route": deque([{"mode": "deep"}]),
            "reasoning:chief_plan": deque([plan("web", write=True)]),
            "reasoning:analysis_decision": deque([{"requests": []}]),
            "reasoning:critic": deque(
                [{"score": 9, "decision": "pass", "gaps": []}]
            ),
            "tool:deep_scout_web": deque(WEB_ACTIONS),
            "data:prepare_write": deque(
                [
                    {
                        "plan_id": "plan_1",
                        "datasource_id": "ds1",
                        "operation": "update",
                        "table": "public.accounts",
                        "values": {"status": "active"},
                        "filters": [{"column": "id", "op": "=", "value": 7}],
                        "reason": "Apply the approved correction",
                    }
                ]
            ),
            "writing:lead_writer": deque([report_response(log)]),
        }
    )
    task_id, paused = run_task(container, "phase8-hitl")
    assert paused.outcome == "paused" and paused.state.pending_write
    pending = container.pending_write_service.get(
        "tenant_test", "user_test", task_id, paused.state.pending_write
    )
    container.pending_write_service.approve(
        tenant_id="tenant_test",
        user_id="user_test",
        task_id=task_id,
        write_id=pending.write_id,
        plan_hash=pending.plan_hash,
        comment="approved",
    )
    written = container.write_executor.execute(pending.write_id, pending.plan_hash)
    assert written.status == "executed"
    completed = container.worker.run(task_id)
    assert completed.outcome == "success"
    assert completed.state.report.final is not None
    assert len(factory.target.calls) == 1
    replay = container.write_executor.execute(pending.write_id, pending.plan_hash)
    assert replay.status == "executed"
    assert replay.operation_key == written.operation_key
    assert len(factory.target.calls) == 1


class SimulatedProcessCrash(BaseException):
    pass


def test_phase8_checkpoint_recovery_reuses_nodes_and_tool_effects(session_factory) -> None:
    container, runtime, log = build_harness(session_factory, {})

    def crash(_prompt: str, _schema: object):
        raise SimulatedProcessCrash()

    runtime._scripted.update(
        {
            "routing:route": deque([{"mode": "deep"}]),
            "reasoning:chief_plan": deque([plan("web")]),
            "reasoning:analysis_decision": deque([crash, {"requests": []}]),
            "reasoning:critic": deque(
                [{"score": 9, "decision": "pass", "gaps": []}]
            ),
            "tool:deep_scout_web": deque(WEB_ACTIONS),
            "writing:lead_writer": deque([report_response(log)]),
        }
    )
    task_id = container.task_service.create(
        "tenant_test",
        "user_test",
        "phase8-recovery",
        CreateTaskRequest(query="research question"),
    ).projection.task_id
    with pytest.raises(SimulatedProcessCrash):
        container.worker.run(task_id)
    calls_before = Counter(log.calls)
    with session_factory() as session, session.begin():
        task = session.get(ResearchTaskRow, task_id)
        task.status = "pending"
        task.lease_token = None
        task.leased_until = None
        task.budget_accounted_at = None
    result = container.worker.run(task_id)
    assert result.outcome == "success"
    assert log.calls == calls_before
    with session_factory() as session:
        branch_runs = session.scalars(
            select(NodeRunRow).where(
                NodeRunRow.task_id == task_id,
                NodeRunRow.node_name == "deep_scout_web",
            )
        ).all()
        assert len(branch_runs) == 1
        effects = session.scalars(
            select(NodeEffectRow).where(NodeEffectRow.task_id == task_id)
        ).all()
        assert len(effects) == 2
        assert len({item.operation_key for item in effects}) == 2


def binding(key: str, purpose: str) -> FrozenModelBinding:
    return FrozenModelBinding(
        binding_key=key,  # type: ignore[arg-type]
        purpose=purpose,  # type: ignore[arg-type]
        primary=ResolvedModelRevision(
            model_config_id=f"model_test_{purpose}", version=1
        ),
    )


def prepare_rag(container) -> None:
    doc_id = container.document_service.create(
        tenant_id="tenant_test",
        name="phase8.txt",
        media_type="text/plain",
        data=b"rag evidence supports claim",
    )
    IngestionService(
        container.session_factory,
        container.document_service,
        container.model_runtime,
        binding("embedding", "embedding"),
        parent_chars=100,
        child_chars=100,
        overlap_chars=0,
    ).run("tenant_test", doc_id)
    child = next(
        item
        for item in container.document_service.index.chunks.values()
        if item.chunk_level == "child"
    )
    container.model_runtime._scripted["tool:deep_scout_rag"] = deque(
        [
            {
                "action": "tool",
                "tool_name": "search_chunks",
                "arguments": {"query": "rag evidence"},
            },
            {
                "action": "tool",
                "tool_name": "read_document",
                "arguments": {"doc_id": doc_id, "chunk_id": child.chunk_id},
            },
            {
                "action": "adopt",
                "adoptions": [
                    {
                        "observation_index": 1,
                        "summary": "document supports claim",
                        "excerpt": "rag evidence",
                        "stance": "support",
                        "relevance": 0.9,
                    }
                ],
            },
        ]
    )


@pytest.mark.parametrize(
    "channels", [("web", "rag"), ("web", "sql"), ("rag", "sql")]
)
def test_phase8_pair_retrieval_combinations_join_only_scheduled_branches(
    session_factory, channels
) -> None:
    shared = {"columns": ["value"], "rows": [[1]], "truncated": False}
    scripted: dict[str, list[object]] = {
        "routing:route": [{"mode": "deep"}],
        "reasoning:chief_plan": [plan(*channels)],
    }
    if "web" in channels:
        scripted["tool:deep_scout_web"] = WEB_ACTIONS
    if "sql" in channels:
        scripted["data:data_analyst_sql"] = SQL_ACTIONS
    container, _, _ = build_harness(
        session_factory,
        scripted,
        phase3=False,
        phase4=False,
        shared_payload=shared,
    )
    if "rag" in channels:
        prepare_rag(container)
    task_id, result = run_task(
        container, "phase8-combo-" + "-".join(channels)
    )
    assert result.outcome == "phase2_evidence_ready"
    round_ = result.state.research_rounds["0"]
    for branch in ("web", "rag", "sql"):
        assert getattr(round_, branch).status == (
            "completed" if branch in channels else "skipped"
        )
    assert {item.source_type for item in result.state.evidence_pool} == set(channels)
    if channels == ("web", "sql"):
        assert len({item.content_hash for item in result.state.evidence_pool}) == 1
        assert len({item.evidence_id for item in result.state.evidence_pool}) == 2
    with session_factory() as session:
        branch_nodes = set(
            session.scalars(
                select(NodeRunRow.node_name).where(NodeRunRow.task_id == task_id)
            ).all()
        ) & {"deep_scout_web", "deep_scout_rag", "data_analyst_sql"}
    expected = {
        {"web": "deep_scout_web", "rag": "deep_scout_rag", "sql": "data_analyst_sql"}[
            item
        ]
        for item in channels
    }
    assert branch_nodes == expected
