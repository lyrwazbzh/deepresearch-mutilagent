from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime, timezone

from sqlalchemy import select

from src.agent.config import Settings
from src.agent.container import build_container
from src.agent.identity import analysis_request_id, content_hash
from src.agent.backends.events import InMemoryEventBroker
from src.agent.llm.runtime import FakeModelRuntime
from src.agent.backends.persistence.models import NodeRunRow, ResearchTaskRow
from src.agent.runtime.tool_runtime import ToolReference, ToolResult
from src.agent.task_service import CreateTaskRequest


class FakeSearch:
    def execute(self, context, arguments):
        del context, arguments
        return ToolResult(status="success", data={"hits": []})


class FakeRead:
    def execute(self, context, arguments):
        del context, arguments
        data = {"content": "authoritative evidence", "page_index": 0}
        return ToolResult(
            status="success",
            summary="authoritative evidence",
            data=data,
            references=[
                ToolReference(
                    source_uri="https://example.test/source",
                    canonical_url="https://example.test/source",
                    retrieved_at=datetime.now(timezone.utc),
                    content_hash=content_hash(data),
                )
            ],
        )


def settings(*, phase3: bool) -> Settings:
    return Settings(
        database_url="sqlite://",
        redis_url="redis://unused",
        celery_broker_url="redis://unused",
        default_tenant_id="tenant_test",
        default_user_id="user_test",
        model_mode="fake",
        phase2_enabled=True,
        phase3_enabled=phase3,
        phase4_enabled=False,
    )


def plan() -> dict:
    return {
        "hypotheses": [
            {
                "hypothesis_id": "h1",
                "claim": "claim",
                "evidence_needed": ["source"],
                "refuted_if": ["counter"],
                "priority": 1,
                "queries": [
                    {"query_id": "q_web", "channel": "web", "text": "web query"}
                ],
            }
        ],
        "deepen_history": [],
    }


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


def make_container(session_factory, scripted, *, phase3: bool):
    runtime = FakeModelRuntime(scripted)
    container = build_container(
        settings(phase3=phase3),
        session_factory=session_factory,
        model_runtime=runtime,
        event_broker=InMemoryEventBroker(),
    )
    container.tool_registry.replace(
        replace(container.tool_registry.get("search_web"), adapter=FakeSearch())
    )
    container.tool_registry.replace(
        replace(container.tool_registry.get("read_webpage"), adapter=FakeRead())
    )
    return container


def create_and_run(container, key: str):
    task_id = container.task_service.create(
        "tenant_test", "user_test", key, CreateTaskRequest(query="deep")
    ).projection.task_id
    return task_id, container.worker.run(task_id)


def test_phase3_no_compute_path_reaches_staged_boundary(session_factory) -> None:
    container = make_container(
        session_factory,
        {
            "routing": [{"mode": "deep"}],
            "reasoning": [
                plan(),
                {"requests": []},
                {"score": 8, "decision": "pass", "gaps": []},
            ],
            "tool": WEB_ACTIONS,
        },
        phase3=True,
    )
    task_id, result = create_and_run(container, "phase3-no-compute")

    assert result.outcome == "phase3_research_approved"
    assert result.state.coverage is not None and result.state.coverage.complete
    assert result.state.critic is not None and result.state.critic.score == 8
    with session_factory() as session:
        task = session.get(ResearchTaskRow, task_id)
        assert task.status == "running"
        nodes = session.scalars(
            select(NodeRunRow.node_name).where(NodeRunRow.task_id == task_id)
        ).all()
        assert "analysis_decision" in nodes
        assert "coverage" in nodes
        assert "critic" in nodes
        assert "phase3_boundary" in nodes
        assert "code_wizard_compute" not in nodes

    before_ids = [item.evidence_id for item in result.state.evidence_pool]
    with session_factory() as session, session.begin():
        task = session.get(ResearchTaskRow, task_id)
        task.status = "pending"
        task.lease_token = None
        task.leased_until = None
        task.budget_accounted_at = None
    replay = container.worker.run(task_id)
    assert replay.outcome == "phase3_research_approved"
    assert [item.evidence_id for item in replay.state.evidence_pool] == before_ids


def test_phase3_compute_is_adopted_merged_and_lineaged(session_factory) -> None:
    phase2 = make_container(
        session_factory,
        {
            "routing": [{"mode": "deep"}],
            "reasoning": [plan()],
            "tool": WEB_ACTIONS,
        },
        phase3=False,
    )
    task_id, phase2_result = create_and_run(phase2, "phase3-compute")
    assert phase2_result.outcome == "phase2_evidence_ready"
    evidence_id = phase2_result.state.evidence_pool[0].evidence_id
    objective = "Render the evidence as a compact table."
    expected_output = "A markdown table artifact."
    analysis_id = analysis_request_id(
        task_id, "h1", objective, [evidence_id], expected_output
    )

    with session_factory() as session, session.begin():
        task = session.get(ResearchTaskRow, task_id)
        task.status = "pending"
        task.lease_token = None
        task.leased_until = None
        task.budget_accounted_at = None

    phase3 = make_container(
        session_factory,
        {
            "reasoning": [
                {
                    "requests": [
                        {
                            "hypothesis_id": "h1",
                            "objective": objective,
                            "input_evidence_ids": [evidence_id],
                            "expected_output": expected_output,
                        }
                    ]
                },
                {"score": 9, "decision": "pass", "gaps": []},
            ],
            "code": [
                {
                    "action": "tool",
                    "tool_name": "render_table",
                    "arguments": {
                        "inline": {
                            "columns": ["metric", "value"],
                            "rows": [["support", 1]],
                        },
                        "evidence_ids": [evidence_id],
                        "title": "Evidence summary",
                        "format": "markdown",
                    },
                },
                {
                    "action": "adopt",
                    "adoptions": [
                        {
                            "observation_index": 0,
                            "summary": "Evidence summary table",
                            "excerpt": "support = 1",
                            "stance": "support",
                            "relevance": 0.9,
                        }
                    ],
                },
            ],
        },
        phase3=True,
    )
    result = phase3.worker.run(task_id)

    assert result.outcome == "phase3_research_approved"
    request = next(item for item in result.state.analysis_requests if item.analysis_id == analysis_id)
    assert request.status == "completed"
    compute = [item for item in result.state.evidence_pool if item.source_type == "compute"]
    assert len(compute) == 1
    assert compute[0].derived_from == [evidence_id]
    assert compute[0].execution_ref.endswith("/manifest.json")
    assert compute[0].workspace_path.endswith("/outputs/table.md")
    manifest = json.loads(phase3.workspace_service.read(
        "tenant_test", task_id, compute[0].execution_ref
    ))
    assert manifest["analysis_id"] == analysis_id
    assert manifest["input_evidence"] == [
        {
            "evidence_id": evidence_id,
            "content_hash": phase2_result.state.evidence_pool[0].content_hash,
        }
    ]
    assert manifest["outputs"][0]["workspace_path"] == compute[0].workspace_path


def test_phase3_critic_deepen_reenters_full_quality_loop(session_factory) -> None:
    container = make_container(
        session_factory,
        {
            "routing": [{"mode": "deep"}],
            "reasoning": [
                plan(),
                {"requests": []},
                {
                    "score": 6,
                    "decision": "deepen",
                    "gaps": [{"hypothesis_id": "h1", "reason": "Need another source"}],
                },
                {
                    "reason": "Add one independent source",
                    "new_hypotheses": [],
                    "query_additions": [
                        {
                            "hypothesis_id": "h1",
                            "evidence_needed": ["independent source"],
                            "queries": [
                                {
                                    "query_id": "q_web_deeper",
                                    "channel": "web",
                                    "text": "independent web source",
                                }
                            ],
                        }
                    ],
                },
                {"requests": []},
                {"score": 9, "decision": "pass", "gaps": []},
            ],
            "tool": [*WEB_ACTIONS, *WEB_ACTIONS],
        },
        phase3=True,
    )

    _, result = create_and_run(container, "phase3-deepen")

    assert result.outcome == "phase3_research_approved"
    assert result.state.execution.depth == 1
    assert len(result.state.plan.deepen_history) == 1
    assert set(result.state.research_rounds) == {"0", "1"}
    assert len(result.state.evidence_pool) == 2


def test_phase3_processes_every_pending_analysis_request_before_coverage(
    session_factory,
) -> None:
    phase2 = make_container(
        session_factory,
        {
            "routing": [{"mode": "deep"}],
            "reasoning": [plan()],
            "tool": WEB_ACTIONS,
        },
        phase3=False,
    )
    task_id, phase2_result = create_and_run(phase2, "phase3-multiple-compute")
    evidence_id = phase2_result.state.evidence_pool[0].evidence_id
    with session_factory() as session, session.begin():
        task = session.get(ResearchTaskRow, task_id)
        task.status = "pending"
        task.lease_token = None
        task.leased_until = None
        task.budget_accounted_at = None

    requests = [
        {
            "hypothesis_id": "h1",
            "objective": f"Render table {index}",
            "input_evidence_ids": [evidence_id],
            "expected_output": f"Table {index}",
        }
        for index in (1, 2)
    ]
    code_actions = []
    for index in (1, 2):
        code_actions.extend(
            [
                {
                    "action": "tool",
                    "tool_name": "render_table",
                    "arguments": {
                        "inline": {
                            "columns": ["metric", "value"],
                            "rows": [[f"request-{index}", index]],
                        },
                        "evidence_ids": [evidence_id],
                        "format": "csv",
                    },
                },
                {
                    "action": "adopt",
                    "adoptions": [
                        {
                            "observation_index": 0,
                            "summary": f"Table {index}",
                            "excerpt": f"request-{index}",
                            "stance": "context",
                            "relevance": 0.8,
                        }
                    ],
                },
            ]
        )
    phase3 = make_container(
        session_factory,
        {
            "reasoning": [
                {"requests": requests},
                {"score": 9, "decision": "pass", "gaps": []},
            ],
            "code": code_actions,
        },
        phase3=True,
    )

    result = phase3.worker.run(task_id)

    assert result.outcome == "phase3_research_approved"
    assert {item.status for item in result.state.analysis_requests} == {"completed"}
    assert len([item for item in result.state.evidence_pool if item.source_type == "compute"]) == 2
    with session_factory() as session:
        compute_nodes = session.scalars(
            select(NodeRunRow).where(
                NodeRunRow.task_id == task_id,
                NodeRunRow.node_name == "code_wizard_compute",
            )
        ).all()
        assert len(compute_nodes) == 2
        assert len({item.node_key for item in compute_nodes}) == 2


def test_phase3_critic_retrieve_reenters_merge_analysis_and_coverage(
    session_factory,
) -> None:
    container = make_container(
        session_factory,
        {
            "routing": [{"mode": "deep"}],
            "reasoning": [
                plan(),
                {"requests": []},
                {
                    "score": 7,
                    "decision": "retrieve",
                    "gaps": [
                        {
                            "hypothesis_id": "h1",
                            "reason": "Find an independent primary source",
                            "target_channel": "web",
                        }
                    ],
                },
                {"requests": []},
                {"score": 9, "decision": "pass", "gaps": []},
            ],
            "tool": [*WEB_ACTIONS, *WEB_ACTIONS],
        },
        phase3=True,
    )

    task_id, result = create_and_run(container, "phase3-retrieve")

    assert result.outcome == "phase3_research_approved"
    assert result.state.execution.depth == 0
    assert len(result.state.evidence_pool) == 2
    with session_factory() as session:
        nodes = session.scalars(
            select(NodeRunRow.node_name)
            .where(NodeRunRow.task_id == task_id)
            .order_by(NodeRunRow.created_at)
        ).all()
        assert nodes.count("merge_evidence") == 2
        assert nodes.count("analysis_decision") == 2
        assert nodes.count("coverage") == 2
        assert nodes.count("critic") == 2


def test_phase3_structural_stop_marks_task_failed(session_factory) -> None:
    container = make_container(
        session_factory,
        {
            "routing": [{"mode": "deep"}],
            "reasoning": [plan(), {"requests": []}],
            "tool": [WEB_ACTIONS[0]],
        },
        phase3=True,
    )
    task_id = container.task_service.create(
        "tenant_test",
        "user_test",
        "phase3-failure",
        CreateTaskRequest(query="deep"),
    ).projection.task_id
    with session_factory() as session, session.begin():
        task = session.get(ResearchTaskRow, task_id)
        state = dict(task.latest_state)
        state["execution"] = {**state["execution"], "max_depth": 0}
        task.latest_state = state

    result = container.worker.run(task_id)

    assert result.outcome == "failure"
    assert result.failure_reason == "max_depth"
    with session_factory() as session:
        task = session.get(ResearchTaskRow, task_id)
        assert task.status == "failed"
        assert task.latest_state["reliability"]["errors"][-1]["reason"] == "max_depth"


def test_phase3_critic_gap_with_exhausted_budget_marks_task_failed(
    session_factory,
) -> None:
    container = make_container(
        session_factory,
        {
            "routing": [{"mode": "deep"}],
            "reasoning": [
                plan(),
                {"requests": []},
                {
                    "score": 6,
                    "decision": "retrieve",
                    "gaps": [
                        {
                            "hypothesis_id": "h1",
                            "reason": "Need another source",
                            "target_channel": "web",
                        }
                    ],
                },
            ],
            "tool": WEB_ACTIONS,
        },
        phase3=True,
    )
    task_id = container.task_service.create(
        "tenant_test",
        "user_test",
        "phase3-budget-failure",
        CreateTaskRequest(query="deep"),
    ).projection.task_id
    with session_factory() as session, session.begin():
        task = session.get(ResearchTaskRow, task_id)
        task_budget = dict(task.task_budget)
        task_budget["budget"] = {
            **task_budget["budget"],
            "max_search_calls": 1,
        }
        task.task_budget = task_budget

    result = container.worker.run(task_id)

    assert result.outcome == "failure"
    assert result.failure_reason == "budget_exhausted"
    with session_factory() as session:
        task = session.get(ResearchTaskRow, task_id)
        assert task.status == "failed"
        assert task.task_budget["usage"]["search_calls"] == 1
