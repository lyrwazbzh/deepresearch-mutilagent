from __future__ import annotations

from sqlalchemy import select

from src.agent.schema.model_config import (
    FrozenModelBinding,
    ModelBindingSnapshot,
    ResolvedModelRevision,
)
from src.agent.llm.runtime import PHASE1_BINDINGS, ModelResolver
from src.agent.evaluation.model_rollout import ToolCanaryAssigner, ToolCanaryPolicy
from src.agent.backends.persistence.models import ResearchTaskRow
from src.agent.task_service import CreateTaskRequest, TaskService


def _candidate(key: str) -> FrozenModelBinding:
    return FrozenModelBinding(
        binding_key=key,  # type: ignore[arg-type]
        purpose="chat",
        primary=ResolvedModelRevision(model_config_id="candidate_tool_model", version=1),
        fallback=[ResolvedModelRevision(model_config_id="model_test", version=1)],
    )


def test_canary_assignment_happens_before_freeze_and_rollback_only_affects_new_tasks(
    session_factory,
) -> None:
    policy = ToolCanaryPolicy(
        version="policy-v1",
        enabled=True,
        rollout_percent=100,
        assignment_salt="stable",
        eligible_tenants=["tenant_test"],
        candidate_bindings={key: _candidate(key) for key in ("tool", "data", "code")},
    )
    candidate_service = TaskService(
        session_factory,
        ModelResolver(),
        required_bindings=PHASE1_BINDINGS,
        tool_canary_assigner=ToolCanaryAssigner(policy),
    )
    first = candidate_service.create(
        "tenant_test",
        "user_test",
        "canary-task",
        CreateTaskRequest(query="candidate task"),
    )
    with session_factory() as session:
        row = session.scalar(
            select(ResearchTaskRow).where(
                ResearchTaskRow.task_id == first.projection.task_id
            )
        )
        assert row is not None
        frozen_before_rollback = ModelBindingSnapshot.model_validate(
            row.model_binding_snapshot
        )
    assert frozen_before_rollback.get("tool").primary.model_config_id == (
        "candidate_tool_model"
    )

    rollback_service = TaskService(
        session_factory,
        ModelResolver(),
        required_bindings=PHASE1_BINDINGS,
        tool_canary_assigner=ToolCanaryAssigner(
            policy.model_copy(update={"rollback": True})
        ),
    )
    second = rollback_service.create(
        "tenant_test",
        "user_test",
        "post-rollback-task",
        CreateTaskRequest(query="baseline task"),
    )
    with session_factory() as session:
        rows = list(
            session.scalars(
                select(ResearchTaskRow).where(
                    ResearchTaskRow.task_id.in_(
                        [first.projection.task_id, second.projection.task_id]
                    )
                )
            )
        )
    by_id = {row.task_id: ModelBindingSnapshot.model_validate(row.model_binding_snapshot) for row in rows}
    assert by_id[first.projection.task_id].get("tool").primary.model_config_id == (
        "candidate_tool_model"
    )
    assert by_id[second.projection.task_id].get("tool").primary.model_config_id == "model_test"
