from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone

from sqlalchemy import select

from src.agent.config import Settings
from src.agent.container import build_container
from src.agent.identity import content_hash
from src.agent.backends.events import InMemoryEventBroker
from src.agent.llm.runtime import FakeModelRuntime
from src.agent.backends.persistence.models import NodeRunRow
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


def plan() -> dict:
    return {
        "hypotheses": [
            {
                "hypothesis_id": "h1",
                "claim": "claim",
                "evidence_needed": ["source"],
                "refuted_if": ["counter"],
                "priority": 1,
                "queries": [{"query_id": "q_web", "channel": "web", "text": "web query"}],
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


def test_plan_and_semantic_deepen_refresh_memory_but_critic_does_not_receive_it(
    session_factory,
) -> None:
    runtime = FakeModelRuntime(
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
        }
    )
    container = build_container(
        Settings(
            database_url="sqlite://",
            redis_url="redis://unused",
            celery_broker_url="redis://unused",
            default_tenant_id="tenant_test",
            default_user_id="user_test",
            model_mode="fake",
            phase2_enabled=True,
            phase3_enabled=True,
            phase4_enabled=False,
            phase5_enabled=True,
        ),
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
    created = container.task_service.create(
        "tenant_test", "user_test", "phase5-deepen", CreateTaskRequest(query="deep")
    )
    assert created.projection.message_id is not None
    container.memory_service.remember(
        tenant_id="tenant_test",
        user_id="user_test",
        topic="output.language.default",
        content="Use concise English output.",
        supporting_message_ids=[created.projection.message_id],
        logical_key="deepen-memory",
    )
    recall_queries: list[str] = []
    original_recall = container.memory_service.recall

    def recording_recall(**kwargs):
        recall_queries.append(str(kwargs["planning_query"]))
        return original_recall(**kwargs)

    container.memory_service.recall = recording_recall  # type: ignore[method-assign]
    result = container.worker.run(created.projection.task_id)

    assert result.outcome == "phase3_research_approved"
    assert len(recall_queries) == 2
    assert recall_queries[0] == "deep"
    assert "Need another source" in recall_queries[1]
    assert sum(
        "Use concise English output." in prompt for prompt in runtime.calls["reasoning"]
    ) == 2
    with session_factory() as session:
        nodes = list(
            session.scalars(
                select(NodeRunRow.node_name).where(
                    NodeRunRow.task_id == created.projection.task_id
                )
            ).all()
        )
    assert nodes.count("memory_recall_plan") == 1
    assert nodes.count("memory_recall_deepen") == 1
