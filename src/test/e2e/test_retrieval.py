from __future__ import annotations

from collections import deque
from dataclasses import replace
from datetime import datetime, timezone

from sqlalchemy import select

from src.agent.tools.documents import IngestionService
from src.agent.config import Settings
from src.agent.container import build_container
from src.agent.identity import content_hash
from src.agent.schema.model_config import FrozenModelBinding, ResolvedModelRevision
from src.agent.backends.events import InMemoryEventBroker
from src.agent.llm.runtime import FakeModelRuntime
from src.agent.backends.persistence.models import NodeRunRow, ResearchTaskRow
from src.agent.runtime.tool_runtime import ToolReference, ToolResult
from src.agent.task_service import CreateTaskRequest


class FakeSearch:
    def execute(self, context, arguments):
        return ToolResult(
            status="success",
            summary="candidate only",
            data={
                "hits": [
                    {
                        "title": "Source",
                        "url": "https://example.test/source",
                        "canonical_url": "https://example.test/source",
                        "snippet": "candidate",
                        "source": "web",
                    }
                ]
            },
        )


class FakeRead:
    def execute(self, context, arguments):
        data = {
            "title": "Source",
            "content": "authoritative evidence supports the hypothesis",
            "page_index": 0,
            "page_size_chars": 12000,
            "has_more": False,
            "next_page_index": None,
            "rendered": False,
        }
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


class FakeSql:
    def execute(self, context, arguments):
        data = {"columns": ["total"], "rows": [[42]], "truncated": False}
        return ToolResult(
            status="success",
            summary="one aggregate row",
            data=data,
            references=[ToolReference(content_hash=content_hash(data))],
        )


def binding(key, purpose):
    return FrozenModelBinding(
        binding_key=key,
        purpose=purpose,
        primary=ResolvedModelRevision(model_config_id=f"model_test_{purpose}", version=1),
    )


def container_for(session_factory, scripted):
    settings = Settings(
        database_url="sqlite://",
        redis_url="redis://unused",
        celery_broker_url="redis://unused",
        default_tenant_id="tenant_test",
        default_user_id="user_test",
        model_mode="fake",
        phase2_enabled=True,
    )
    runtime = FakeModelRuntime(scripted)
    container = build_container(
        settings,
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
    container.tool_registry.replace(
        replace(container.tool_registry.get("execute_readonly_sql"), adapter=FakeSql())
    )
    return container, runtime


def plan(*channels):
    return {
        "hypotheses": [
            {
                "hypothesis_id": "h1",
                "claim": "claim",
                "evidence_needed": ["source"],
                "refuted_if": ["counter"],
                "priority": 1,
                "queries": [
                    {"query_id": f"q_{channel}", "channel": channel, "text": f"{channel} query"}
                    for channel in channels
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

SQL_ACTIONS = [
    {
        "action": "tool",
        "tool_name": "execute_readonly_sql",
        "arguments": {"sql": "SELECT 42 AS total"},
    },
    {
        "action": "adopt",
        "adoptions": [
            {
                "observation_index": 0,
                "summary": "aggregate supports claim",
                "excerpt": "total=42",
                "stance": "support",
                "relevance": 0.9,
            }
        ],
    },
]


def prepare_rag(container) -> None:
    doc_id = container.document_service.create(
        tenant_id="tenant_test",
        name="doc.txt",
        media_type="text/plain",
        data=b"rag evidence supports claim",
    )
    ingestion = IngestionService(
        container.session_factory,
        container.document_service,
        container.model_runtime,
        binding("embedding", "embedding"),
        parent_chars=100,
        child_chars=100,
        overlap_chars=0,
    )
    ingestion.run("tenant_test", doc_id)
    child = next(
        item
        for item in container.document_service.index.chunks.values()
        if item.chunk_level == "child"
    )
    container.model_runtime._scripted.setdefault(
        "tool:deep_scout_rag", deque()
    ).extend(
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


def run(container, key):
    task_id = container.task_service.create(
        "tenant_test", "user_test", key, CreateTaskRequest(query="deep")
    ).projection.task_id
    result = container.worker.run(task_id)
    return task_id, result


def assert_boundary(session_factory, task_id, branches, evidence_types):
    with session_factory() as session:
        task = session.get(ResearchTaskRow, task_id)
        state = task.latest_state
        assert task.status == "running"
        assert {name: state["research_rounds"]["0"][name]["status"] for name in branches} == branches
        assert {item["source_type"] for item in state["evidence_pool"]} == evidence_types
        nodes = set(
            session.scalars(select(NodeRunRow.node_name).where(NodeRunRow.task_id == task_id)).all()
        )
        assert {"retrieval_dispatch", "retrieval_join", "merge_evidence"}.issubset(nodes)
        active_branch_nodes = list(
            session.scalars(
                select(NodeRunRow).where(
                    NodeRunRow.task_id == task_id,
                    NodeRunRow.node_name.in_(
                        ["deep_scout_web", "deep_scout_rag", "data_analyst_sql"]
                    ),
                )
            ).all()
        )
        if len(active_branch_nodes) > 1:
            assert len({item.input_checkpoint_id for item in active_branch_nodes}) == 1


def test_deep_web_to_evidence_pool(session_factory) -> None:
    container, _ = container_for(
        session_factory,
        {"routing": [{"mode": "deep"}], "reasoning": [plan("web")], "tool": WEB_ACTIONS},
    )
    task_id, result = run(container, "web")
    assert result.outcome == "phase2_evidence_ready"
    assert_boundary(
        session_factory,
        task_id,
        {"web": "completed", "rag": "skipped", "sql": "skipped"},
        {"web"},
    )


def test_same_web_result_supports_two_hypotheses(session_factory) -> None:
    two_hypotheses = plan("web")
    two_hypotheses["hypotheses"].append(
        {
            "hypothesis_id": "h2",
            "claim": "second claim",
            "evidence_needed": ["same source"],
            "refuted_if": ["counter"],
            "priority": 2,
            "queries": [{"query_id": "q_web_2", "channel": "web", "text": "web query"}],
        }
    )
    container, _ = container_for(
        session_factory,
        {
            "routing": [{"mode": "deep"}],
            "reasoning": [two_hypotheses],
            "tool": [*WEB_ACTIONS, *WEB_ACTIONS],
        },
    )
    task_id, _ = run(container, "two-hypotheses")
    with session_factory() as session:
        task = session.get(ResearchTaskRow, task_id)
        evidence = task.latest_state["evidence_pool"]
        assert {item["hypothesis_id"] for item in evidence} == {"h1", "h2"}
        assert len({item["evidence_id"] for item in evidence}) == 2
        assert len({item["content_hash"] for item in evidence}) == 1


def test_deep_sql_to_evidence_pool(session_factory) -> None:
    container, _ = container_for(
        session_factory,
        {"routing": [{"mode": "deep"}], "reasoning": [plan("sql")], "data": SQL_ACTIONS},
    )
    task_id, result = run(container, "sql")
    assert result.outcome == "phase2_evidence_ready"
    assert_boundary(
        session_factory,
        task_id,
        {"web": "skipped", "rag": "skipped", "sql": "completed"},
        {"sql"},
    )


def test_deep_rag_to_evidence_pool(session_factory) -> None:
    container, _ = container_for(
        session_factory,
        {"routing": [{"mode": "deep"}], "reasoning": [plan("rag")]},
    )
    prepare_rag(container)
    task_id, result = run(container, "rag")
    assert result.outcome == "phase2_evidence_ready"
    assert_boundary(
        session_factory,
        task_id,
        {"web": "skipped", "rag": "completed", "sql": "skipped"},
        {"rag"},
    )


def test_deep_all_branches_to_evidence_pool(session_factory) -> None:
    container, _ = container_for(
        session_factory,
        {
            "routing": [{"mode": "deep"}],
            "reasoning": [plan("web", "rag", "sql")],
            "tool": [*WEB_ACTIONS],
            "data": SQL_ACTIONS,
        },
    )
    prepare_rag(container)
    task_id, result = run(container, "all")
    assert result.outcome == "phase2_evidence_ready"
    assert_boundary(
        session_factory,
        task_id,
        {"web": "completed", "rag": "completed", "sql": "completed"},
        {"web", "rag", "sql"},
    )


def test_failed_branch_still_reaches_merge_boundary(session_factory) -> None:
    container, _ = container_for(
        session_factory,
        {
            "routing": [{"mode": "deep"}],
            "reasoning": [plan("web", "sql")],
            "tool": [WEB_ACTIONS[0]],
            "data": SQL_ACTIONS,
        },
    )
    task_id, result = run(container, "failure")
    assert result.outcome == "phase2_evidence_ready"
    assert_boundary(
        session_factory,
        task_id,
        {"web": "failed", "rag": "skipped", "sql": "completed"},
        {"sql"},
    )


def test_phase2_boundary_recovery_replays_merge_without_duplicate(session_factory) -> None:
    container, _ = container_for(
        session_factory,
        {"routing": [{"mode": "deep"}], "reasoning": [plan("web")], "tool": WEB_ACTIONS},
    )
    task_id, _ = run(container, "recovery")
    with session_factory() as session, session.begin():
        task = session.get(ResearchTaskRow, task_id)
        before = [item["evidence_id"] for item in task.latest_state["evidence_pool"]]
        task.status = "pending"
        task.lease_token = None
        task.leased_until = None
        task.budget_accounted_at = None

    replay = container.worker.run(task_id)
    assert replay.outcome == "phase2_evidence_ready"
    with session_factory() as session:
        task = session.get(ResearchTaskRow, task_id)
        after = [item["evidence_id"] for item in task.latest_state["evidence_pool"]]
        assert after == before
        assert len(after) == 1
