from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import pytest

from src.agent.schema.runtime import StatePatch
from src.agent.backends.persistence.models import NodeRunRow, ResearchCheckpointRow, ResearchTaskRow
from src.agent.errors import PatchOwnershipError
from src.agent.task_service import CreateTaskRequest


def setup(container, session_factory):
    task_id = container.task_service.create(
        "tenant_test", "user_test", "ownership", CreateTaskRequest(query="q")
    ).projection.task_id
    task, token = container.worker.claim(task_id)
    with session_factory() as session:
        checkpoint = session.query(ResearchCheckpointRow).filter_by(task_id=task_id).one()
    return task, token, checkpoint.checkpoint_id


@pytest.mark.parametrize("node_name", ["deep_scout", "data_analyst", "code_wizard"])
def test_execution_agents_cannot_patch_evidence_pool(
    make_container, session_factory, node_name
) -> None:
    container, _ = make_container({})
    task, token, checkpoint = setup(container, session_factory)
    with session_factory() as session, session.begin():
        session.add(
            NodeRunRow(
                node_run_id=f"node_{node_name}",
                tenant_id=task.tenant_id,
                task_id=task.task_id,
                node_key=f"key_{node_name}",
                node_name=node_name,
                input_checkpoint_id=checkpoint,
                status="running",
                attempt_count=1,
            )
        )
    with pytest.raises(PatchOwnershipError):
        container.state_manager.commit_node(
            f"node_{node_name}",
            StatePatch(values={"evidence_pool": []}),
            token,
        )


def test_merge_evidence_is_only_evidence_writer(make_container, session_factory) -> None:
    container, _ = make_container({})
    task, token, checkpoint = setup(container, session_factory)
    with session_factory() as session, session.begin():
        session.add(
            NodeRunRow(
                node_run_id="node_merge",
                tenant_id=task.tenant_id,
                task_id=task.task_id,
                node_key="merge",
                node_name="merge_evidence",
                input_checkpoint_id=checkpoint,
                status="running",
                attempt_count=1,
            )
        )
    container.state_manager.commit_node(
        "node_merge", StatePatch(list_appends={"evidence_pool": []}), token
    )


def test_merge_evidence_cannot_mutate_unrelated_fields(make_container, session_factory) -> None:
    container, _ = make_container({})
    task, token, checkpoint = setup(container, session_factory)
    with session_factory() as session, session.begin():
        session.add(
            NodeRunRow(
                node_run_id="node_merge_forbidden",
                tenant_id=task.tenant_id,
                task_id=task.task_id,
                node_key="merge_forbidden",
                node_name="merge_evidence",
                input_checkpoint_id=checkpoint,
                status="running",
                attempt_count=1,
            )
        )
    with pytest.raises(PatchOwnershipError):
        container.state_manager.commit_node(
            "node_merge_forbidden",
            StatePatch(values={"plan": None}),
            token,
        )


def test_branch_owner_cannot_mutate_sibling_branch(make_container, session_factory) -> None:
    container, _ = make_container({})
    task, token, checkpoint = setup(container, session_factory)
    with session_factory() as session, session.begin():
        session.add(
            NodeRunRow(
                node_run_id="node_web",
                tenant_id=task.tenant_id,
                task_id=task.task_id,
                node_key="web",
                node_name="deep_scout_web",
                input_checkpoint_id=checkpoint,
                status="running",
                attempt_count=1,
            )
        )
    with pytest.raises(PatchOwnershipError):
        container.state_manager.commit_node(
            "node_web",
            StatePatch(
                values={
                    "research_rounds": {
                        "0": {
                            "web": {"status": "pending"},
                            "rag": {"status": "skipped"},
                            "sql": {"status": "completed"},
                        }
                    }
                }
            ),
            token,
        )


def test_concurrent_branch_updates_do_not_lose_siblings(
    make_container, session_factory
) -> None:
    container, _ = make_container({})
    task, token, checkpoint = setup(container, session_factory)
    with session_factory() as session, session.begin():
        session.add(
            NodeRunRow(
                node_run_id="node_dispatch",
                tenant_id=task.tenant_id,
                task_id=task.task_id,
                node_key="dispatch",
                node_name="retrieval_dispatch",
                input_checkpoint_id=checkpoint,
                status="running",
                attempt_count=1,
            )
        )
    dispatched = container.state_manager.commit_node(
        "node_dispatch",
        StatePatch(
            values={
                "research_rounds": {
                    "0": {
                        "web": {"status": "pending", "items": []},
                        "rag": {"status": "pending", "items": []},
                        "sql": {"status": "pending", "items": []},
                    }
                }
            }
        ),
        token,
    )
    nodes = {
        "web": ("node_web_concurrent", "deep_scout_web"),
        "rag": ("node_rag_concurrent", "deep_scout_rag"),
        "sql": ("node_sql_concurrent", "data_analyst_sql"),
    }
    with session_factory() as session, session.begin():
        for branch, (node_run_id, node_name) in nodes.items():
            session.add(
                NodeRunRow(
                    node_run_id=node_run_id,
                    tenant_id=task.tenant_id,
                    task_id=task.task_id,
                    node_key=f"concurrent_{branch}",
                    node_name=node_name,
                    input_checkpoint_id=dispatched.checkpoint_id,
                    status="running",
                    attempt_count=1,
                )
            )

    def commit(branch: str) -> None:
        node_run_id, _ = nodes[branch]
        container.state_manager.commit_node(
            node_run_id,
            StatePatch(
                branch_updates={
                    "0": {branch: {"status": "completed", "items": []}}
                }
            ),
            token,
        )

    with ThreadPoolExecutor(max_workers=3) as executor:
        list(executor.map(commit, nodes))

    with session_factory() as session:
        latest = session.get(ResearchTaskRow, task.task_id).latest_state
    round_ = latest["research_rounds"]["0"]
    assert {round_[branch]["status"] for branch in nodes} == {"completed"}
