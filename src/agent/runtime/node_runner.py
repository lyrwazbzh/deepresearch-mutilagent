from __future__ import annotations

from collections.abc import Callable
from contextlib import nullcontext
from datetime import datetime, timezone

from sqlalchemy import select

from src.agent.schema.task import TaskBudget
from src.agent.schema.runtime import ContextPacket, ExecutionContext, NodeExecution, NodeResult
from src.agent.schema.state import ResearchState
from src.agent.backends.persistence.database import SessionFactory
from src.agent.backends.persistence.models import NodeRunRow, ResearchTaskRow, utcnow
from src.agent.backends.observability import Telemetry, TraceScope
from src.agent.runtime.budget import bind_runtime_effect_context
from src.agent.runtime.context import ContextBuilder
from src.agent.runtime.state_manager import StateManager, _aware
from src.agent.errors import LeaseLostError, TaskCancelRequested, TaskPauseRequested
from src.agent.task_service import new_id


NodeHandler = Callable[[ContextPacket, int], NodeResult]


class NodeRunner:
    def __init__(
        self,
        session_factory: SessionFactory,
        context_builder: ContextBuilder,
        state_manager: StateManager,
        telemetry: Telemetry | None = None,
    ) -> None:
        self.session_factory = session_factory
        self.context_builder = context_builder
        self.state_manager = state_manager
        self.telemetry = telemetry

    def execute(
        self,
        *,
        node_name: str,
        node_key: str,
        context_role: str,
        input_checkpoint_id: str,
        execution: ExecutionContext,
        state: ResearchState,
        budget: TaskBudget,
        handler: NodeHandler,
    ) -> NodeExecution:
        with self.session_factory() as session, session.begin():
            task = session.scalar(
                select(ResearchTaskRow)
                .where(
                    ResearchTaskRow.tenant_id == execution.tenant_id,
                    ResearchTaskRow.task_id == execution.task_id,
                )
                .with_for_update()
            )
            now = utcnow()
            leased_until = _aware(task.leased_until) if task is not None else None
            if (
                task is None
                or task.lease_token != execution.lease_token
                or leased_until is None
                or leased_until <= now
            ):
                raise LeaseLostError("Cannot begin a node with a stale task lease.")
            if task.status == "pausing":
                raise TaskPauseRequested()
            if task.status in {"cancelling", "cancelled"}:
                raise TaskCancelRequested()
            if task.status != "running":
                raise LeaseLostError("Cannot begin a node for an inactive task.")
            node_run = session.scalar(
                select(NodeRunRow)
                .where(
                    NodeRunRow.task_id == execution.task_id,
                    NodeRunRow.node_key == node_key,
                    NodeRunRow.input_checkpoint_id == input_checkpoint_id,
                )
                .with_for_update()
            )
            if node_run is None:
                node_run = NodeRunRow(
                    node_run_id=new_id("node"),
                    tenant_id=execution.tenant_id,
                    task_id=execution.task_id,
                    node_key=node_key,
                    node_name=node_name,
                    input_checkpoint_id=input_checkpoint_id,
                    status="pending",
                    attempt_count=0,
                )
                session.add(node_run)
                session.flush()
            if node_run.status == "completed":
                assert node_run.result_json is not None
                result = NodeResult.model_validate(node_run.result_json)
                current = ResearchState.model_validate(task.latest_state)
                return NodeExecution(
                    node_run_id=node_run.node_run_id,
                    node_attempt_no=node_run.attempt_count,
                    replayed=True,
                    result=result,
                    checkpoint_id=node_run.output_checkpoint_id,
                    state=current.model_dump(mode="json"),
                )
            node_run.attempt_count += 1
            node_run.status = "running"
            node_run.updated_at = now
            attempt = node_run.attempt_count
            node_run_id = node_run.node_run_id

        if execution.cancellation_token.is_cancelled():
            raise TaskCancelRequested()
        if datetime.now(timezone.utc) >= execution.deadline:
            self._mark_failed(node_run_id, execution.lease_token)
            raise RuntimeError("Execution deadline expired before node invocation")
        packet = self.context_builder.build(context_role, state, execution, budget)  # type: ignore[arg-type]
        packet.payload["runtime"] = {
            "node_run_id": node_run_id,
            "node_attempt_no": attempt,
        }
        span_context = (
            self.telemetry.span(
                "graph.node",
                scope=TraceScope(
                    tenant_id=execution.tenant_id,
                    user_id=execution.user_id,
                    task_id=execution.task_id,
                ),
                attributes={
                    "research.graph_node": node_name,
                    "research.node_run_id": node_run_id,
                    "research.node_attempt_no": attempt,
                    "research.agent": context_role,
                    "research.depth": state.execution.depth,
                    "research.revision_round": state.execution.revision_round,
                },
            )
            if self.telemetry is not None
            else nullcontext()
        )
        try:
            with span_context:
                with bind_runtime_effect_context(execution, node_run_id):
                    result = handler(packet, attempt)
                outcome = self.state_manager.commit_node(
                    node_run_id,
                    result.patch,
                    execution.lease_token,
                    effects=result.effects,
                    output=result.output,
                )
        except Exception:
            self._mark_failed(node_run_id, execution.lease_token)
            raise
        return NodeExecution(
            node_run_id=node_run_id,
            node_attempt_no=attempt,
            replayed=False,
            result=result,
            checkpoint_id=outcome.checkpoint_id,
            state=outcome.state.model_dump(mode="json"),
        )

    def _mark_failed(self, node_run_id: str, expected_lease_token: str) -> None:
        with self.session_factory() as session, session.begin():
            node_run = session.scalar(
                select(NodeRunRow).where(NodeRunRow.node_run_id == node_run_id).with_for_update()
            )
            if node_run is None:
                return
            task = session.scalar(
                select(ResearchTaskRow)
                .where(ResearchTaskRow.task_id == node_run.task_id)
                .with_for_update()
            )
            if (
                task is not None
                and task.status == "running"
                and task.lease_token == expected_lease_token
                and node_run.status != "completed"
            ):
                node_run.status = "failed"
                node_run.updated_at = utcnow()
