from __future__ import annotations

from datetime import datetime, timezone

import pytest

from src.agent.identity import analysis_request_id, content_hash
from src.agent.schema.runtime import StatePatch
from src.agent.backends.persistence.models import NodeRunRow, ResearchCheckpointRow, ResearchTaskRow
from src.agent.errors import PatchOwnershipError
from src.agent.task_service import CreateTaskRequest


def prepared_task(container, session_factory):
    task_id = container.task_service.create(
        "tenant_test", "user_test", "phase3-ownership", CreateTaskRequest(query="q")
    ).projection.task_id
    task, token = container.worker.claim(task_id)
    data = {"value": 1}
    with session_factory() as session, session.begin():
        row = session.get(ResearchTaskRow, task_id)
        state = dict(row.latest_state)
        state.update(
            {
                "route": {"mode": "deep", "target_agent": None},
                "plan": {
                    "hypotheses": [
                        {
                            "hypothesis_id": "h1",
                            "claim": "claim",
                            "evidence_needed": ["source"],
                            "refuted_if": ["counter"],
                            "priority": 1,
                            "queries": [
                                {
                                    "query_id": "q1",
                                    "channel": "web",
                                    "text": "source",
                                }
                            ],
                        }
                    ],
                    "deepen_history": [],
                },
                "research_rounds": {
                    "0": {
                        "web": {"status": "completed", "items": []},
                        "rag": {"status": "skipped", "items": []},
                        "sql": {"status": "skipped", "items": []},
                    }
                },
                "evidence_pool": [
                    {
                        "evidence_id": "e1",
                        "hypothesis_id": "h1",
                        "summary": "summary",
                        "excerpt": "excerpt",
                        "source_type": "web",
                        "stance": "support",
                        "relevance": 1,
                        "data": data,
                        "source_uri": "https://example.test",
                        "retrieved_at": datetime.now(timezone.utc).isoformat(),
                        "content_hash": content_hash(data),
                    }
                ],
            }
        )
        row.latest_state = state
        checkpoint = session.query(ResearchCheckpointRow).filter_by(task_id=task_id).one()
    return task, token, checkpoint.checkpoint_id


def add_node(session_factory, task, node_run_id, node_name, checkpoint):
    with session_factory() as session, session.begin():
        session.add(
            NodeRunRow(
                node_run_id=node_run_id,
                tenant_id=task.tenant_id,
                task_id=task.task_id,
                node_key=node_run_id,
                node_name=node_name,
                input_checkpoint_id=checkpoint,
                status="running",
                attempt_count=1,
            )
        )


def request_payload(task_id):
    objective = "compute"
    expected = "table"
    return {
        "analysis_id": analysis_request_id(
            task_id, "h1", objective, ["e1"], expected
        ),
        "hypothesis_id": "h1",
        "objective": objective,
        "input_evidence_ids": ["e1"],
        "expected_output": expected,
        "status": "pending",
    }


def test_analysis_decision_is_the_only_analysis_request_creator(
    make_container, session_factory
) -> None:
    container, _ = make_container({})
    task, token, checkpoint = prepared_task(container, session_factory)
    payload = request_payload(task.task_id)
    add_node(session_factory, task, "node_analysis", "analysis_decision", checkpoint)

    outcome = container.state_manager.commit_node(
        "node_analysis",
        StatePatch(analysis_request_appends=[payload]),
        token,
    )
    assert outcome.state.analysis_requests[0].analysis_id == payload["analysis_id"]

    add_node(
        session_factory,
        task,
        "node_critic_create",
        "critic",
        outcome.checkpoint_id,
    )
    with pytest.raises(PatchOwnershipError):
        container.state_manager.commit_node(
            "node_critic_create",
            StatePatch(analysis_request_appends=[payload]),
            token,
        )


def test_code_wizard_can_only_update_status_and_its_compute_branch(
    make_container, session_factory
) -> None:
    container, _ = make_container({})
    task, token, checkpoint = prepared_task(container, session_factory)
    payload = request_payload(task.task_id)
    add_node(session_factory, task, "node_analysis_2", "analysis_decision", checkpoint)
    created = container.state_manager.commit_node(
        "node_analysis_2",
        StatePatch(analysis_request_appends=[payload]),
        token,
    )

    add_node(
        session_factory,
        task,
        "node_compute",
        "code_wizard_compute",
        created.checkpoint_id,
    )
    completed = container.state_manager.commit_node(
        "node_compute",
        StatePatch(
            branch_updates={"0": {"compute": {"status": "completed", "items": []}}},
            analysis_status_updates={payload["analysis_id"]: "completed"},
        ),
        token,
    )
    assert completed.state.analysis_requests[0].status == "completed"
    assert completed.state.analysis_requests[0].objective == "compute"
    assert completed.state.research_rounds["0"].compute.status == "completed"

    add_node(
        session_factory,
        task,
        "node_compute_forbidden",
        "code_wizard_compute",
        completed.checkpoint_id,
    )
    mutated = [{**payload, "objective": "rewritten", "status": "completed"}]
    with pytest.raises(PatchOwnershipError):
        container.state_manager.commit_node(
            "node_compute_forbidden",
            StatePatch(values={"analysis_requests": mutated}),
            token,
        )
