from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from threading import Event, Thread
from contextlib import nullcontext

from sqlalchemy import func, select

from src.agent.schema.model_config import ModelBindingSnapshot
from src.agent.schema.task import TaskBudget
from src.agent.schema.runtime import ExecutionContext
from src.agent.schema.state import ResearchState
from src.agent.main_agent import GraphRunResult, ResearchGraph
from src.agent.backends.persistence.database import SessionFactory
from src.agent.backends.persistence.models import (
    OutboxMessageRow,
    ResearchCheckpointRow,
    ResearchEventRow,
    ResearchTaskRow,
    utcnow,
)
from src.agent.backends.observability import Telemetry, TraceScope
from src.agent.errors import (
    InvalidStateError,
    LeaseLostError,
    TaskCancelRequested,
    TaskPauseRequested,
)
from src.agent.task_service import new_id


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


class DatabaseCancellationToken:
    def __init__(self, session_factory: SessionFactory, task_id: str) -> None:
        self.session_factory = session_factory
        self.task_id = task_id

    def is_cancelled(self) -> bool:
        with self.session_factory() as session:
            status = session.scalar(
                select(ResearchTaskRow.status).where(ResearchTaskRow.task_id == self.task_id)
            )
            return status in {"cancelling", "cancelled"}


class ResearchWorker:
    def __init__(
        self,
        session_factory: SessionFactory,
        graph: ResearchGraph,
        *,
        lease_seconds: int = 300,
        tenant_max_running: int = 4,
        user_max_running: int = 2,
        telemetry: Telemetry | None = None,
    ) -> None:
        self.session_factory = session_factory
        self.graph = graph
        self.lease_seconds = lease_seconds
        self.tenant_max_running = tenant_max_running
        self.user_max_running = user_max_running
        self.telemetry = telemetry

    def claim(self, task_id: str) -> tuple[ResearchTaskRow, str]:
        now = utcnow()
        with self.session_factory() as session, session.begin():
            task = session.scalar(
                select(ResearchTaskRow)
                .where(ResearchTaskRow.task_id == task_id)
                .with_for_update()
            )
            if task is None or task.deleted_at is not None:
                raise InvalidStateError("Task is not claimable.")
            if task.status != "pending":
                raise InvalidStateError(f"Task status {task.status} is not claimable.")
            if session.bind is not None and session.bind.dialect.name == "postgresql":
                session.execute(
                    select(func.pg_advisory_xact_lock(func.hashtext(f"tenant:{task.tenant_id}")))
                )
                session.execute(
                    select(
                        func.pg_advisory_xact_lock(
                            func.hashtext(f"user:{task.tenant_id}:{task.user_id}")
                        )
                    )
                )
            tenant_running = session.scalar(
                select(func.count()).select_from(ResearchTaskRow).where(
                    ResearchTaskRow.tenant_id == task.tenant_id,
                    ResearchTaskRow.status == "running",
                )
            )
            user_running = session.scalar(
                select(func.count()).select_from(ResearchTaskRow).where(
                    ResearchTaskRow.tenant_id == task.tenant_id,
                    ResearchTaskRow.user_id == task.user_id,
                    ResearchTaskRow.status == "running",
                )
            )
            if int(tenant_running or 0) >= self.tenant_max_running:
                raise InvalidStateError("Tenant running task quota is exhausted.")
            if int(user_running or 0) >= self.user_max_running:
                raise InvalidStateError("User running task quota is exhausted.")
            lease_token = uuid.uuid4().hex
            task.status = "running"
            task.lease_token = lease_token
            task.leased_until = now + timedelta(seconds=self.lease_seconds)
            task.budget_accounted_at = now
            task.updated_at = now
            session.flush()
            session.expunge(task)
            return task, lease_token

    def run(self, task_id: str) -> GraphRunResult:
        task, lease_token = self.claim(task_id)
        budget = TaskBudget.model_validate(task.task_budget)
        remaining_ms = budget.remaining_projection()["wall_time_ms"]
        assert isinstance(remaining_ms, int)
        deadline = utcnow() + timedelta(milliseconds=remaining_ms)
        execution = ExecutionContext(
            tenant_id=task.tenant_id,
            user_id=task.user_id,
            task_id=task.task_id,
            thread_id=task.thread_id,
            lease_token=lease_token,
            cancellation_token=DatabaseCancellationToken(self.session_factory, task.task_id),
            deadline=deadline,
            capability_snapshot=dict(task.capability_snapshot),
        )
        with self.session_factory() as session:
            checkpoint = session.scalar(
                select(ResearchCheckpointRow)
                .where(ResearchCheckpointRow.task_id == task_id)
                .order_by(ResearchCheckpointRow.checkpoint_seq.desc())
                .limit(1)
            )
            if checkpoint is None:
                raise RuntimeError("Task has no checkpoint_0")
            state = ResearchState.model_validate(task.latest_state)
            bindings = ModelBindingSnapshot.model_validate(task.model_binding_snapshot)
            checkpoint_id = checkpoint.checkpoint_id
        heartbeat_stop = Event()
        heartbeat = Thread(
            target=self._heartbeat_loop,
            args=(task_id, lease_token, heartbeat_stop),
            name=f"research-heartbeat-{task_id}",
            daemon=True,
        )
        heartbeat.start()
        run_span = (
            self.telemetry.span(
                "research.run",
                scope=TraceScope(
                    tenant_id=task.tenant_id,
                    user_id=task.user_id,
                    task_id=task.task_id,
                ),
                attributes={"research.depth": state.execution.depth},
            )
            if self.telemetry is not None
            else nullcontext()
        )
        try:
            with run_span:
                try:
                    result = self.graph.run(
                        state=state,
                        execution=execution,
                        budget=budget,
                        bindings=bindings,
                        input_checkpoint_id=checkpoint_id,
                    )
                    control_status = self._active_status(task_id, lease_token)
                    if control_status == "pausing":
                        self._finish(task_id, lease_token, "paused", None)
                        return self._snapshot_result(task_id, "paused")
                    if control_status == "cancelling":
                        self._finish(task_id, lease_token, "cancelled", None)
                        return self._snapshot_result(task_id, "cancelled")
                    if result.outcome == "success":
                        self._complete(task_id, lease_token)
                    elif result.outcome == "failure":
                        self._fail(
                            task_id,
                            lease_token,
                            result.failure_reason or "ResearchQualityFailure",
                        )
                    # Staged deep boundaries deliberately keep the task resumable/running.
                    return result
                except TaskPauseRequested:
                    self._finish(task_id, lease_token, "paused", None)
                    return self._snapshot_result(task_id, "paused")
                except TaskCancelRequested:
                    self._finish(task_id, lease_token, "cancelled", None)
                    return self._snapshot_result(task_id, "cancelled")
                except Exception as exc:
                    self._fail(task_id, lease_token, type(exc).__name__)
                    raise
        finally:
            heartbeat_stop.set()
            heartbeat.join(timeout=max(1.0, min(5.0, self.lease_seconds / 3)))

    def _heartbeat_loop(
        self, task_id: str, lease_token: str, stop: Event
    ) -> None:
        interval = max(0.1, self.lease_seconds / 3)
        while not stop.wait(interval):
            if not self.refresh_lease(task_id, lease_token):
                return

    def refresh_lease(self, task_id: str, lease_token: str) -> bool:
        now = utcnow()
        with self.session_factory() as session, session.begin():
            task = session.scalar(
                select(ResearchTaskRow)
                .where(ResearchTaskRow.task_id == task_id)
                .with_for_update()
            )
            leased_until = _aware(task.leased_until) if task is not None else None
            if (
                task is None
                or task.lease_token != lease_token
                or task.status not in {"running", "pausing", "cancelling"}
                or leased_until is None
                or leased_until <= now
            ):
                return False
            budget = TaskBudget.model_validate(task.task_budget)
            accounted = _aware(task.budget_accounted_at)
            if accounted is not None:
                budget.usage.wall_time_ms += max(
                    0, int((now - accounted).total_seconds() * 1000)
                )
            if budget.usage.wall_time_ms >= budget.budget.max_wall_time_ms:
                task.status = "cancelling"
            task.task_budget = budget.model_dump(mode="json")
            task.budget_accounted_at = now
            task.leased_until = now + timedelta(seconds=self.lease_seconds)
            task.updated_at = now
            return True

    def _active_status(self, task_id: str, lease_token: str) -> str | None:
        with self.session_factory() as session:
            task = session.scalar(
                select(ResearchTaskRow).where(ResearchTaskRow.task_id == task_id)
            )
            if task is None or task.lease_token != lease_token:
                return None
            return task.status

    def _snapshot_result(self, task_id: str, outcome: str) -> GraphRunResult:
        with self.session_factory() as session:
            task = session.scalar(
                select(ResearchTaskRow).where(ResearchTaskRow.task_id == task_id)
            )
            checkpoint = session.scalar(
                select(ResearchCheckpointRow)
                .where(ResearchCheckpointRow.task_id == task_id)
                .order_by(ResearchCheckpointRow.checkpoint_seq.desc())
                .limit(1)
            )
            if task is None or checkpoint is None:
                raise RuntimeError("Task checkpoint disappeared during control handling")
            return GraphRunResult(
                outcome=outcome,  # type: ignore[arg-type]
                state=ResearchState.model_validate(task.latest_state),
                checkpoint_id=checkpoint.checkpoint_id,
            )

    def _complete(self, task_id: str, lease_token: str) -> None:
        self._finish(task_id, lease_token, "completed", None)

    def _fail(self, task_id: str, lease_token: str, reason: str) -> None:
        try:
            self._finish(task_id, lease_token, "failed", reason)
        except LeaseLostError:
            pass

    def _finish(
        self, task_id: str, lease_token: str, status: str, reason: str | None
    ) -> None:
        now = utcnow()
        with self.session_factory() as session, session.begin():
            task = session.scalar(
                select(ResearchTaskRow)
                .where(ResearchTaskRow.task_id == task_id)
                .with_for_update()
            )
            allowed_sources = {
                "completed": {"running"},
                "failed": {"running"},
                "paused": {"pausing"},
                "cancelled": {"cancelling"},
            }
            if task is not None and task.status == status:
                return
            if (
                task is None
                or task.status not in allowed_sources.get(status, set())
                or task.lease_token != lease_token
            ):
                raise LeaseLostError("Worker cannot finish a task after losing its lease.")
            budget = TaskBudget.model_validate(task.task_budget)
            accounted = _aware(task.budget_accounted_at)
            if accounted is not None:
                elapsed_ms = max(0, int((now - accounted).total_seconds() * 1000))
                budget.usage.wall_time_ms += elapsed_ms
            task.task_budget = budget.model_dump(mode="json")
            task.status = status
            task.lease_token = None
            task.leased_until = None
            task.budget_accounted_at = None
            task.updated_at = now
            event = ResearchEventRow(
                tenant_id=task.tenant_id,
                task_id=task.task_id,
                event_type="task_updated",
                payload_json={
                    "event_id": 0,
                    "task_id": task.task_id,
                    "changed": ["task_status", "phase"],
                    "at": now.isoformat(),
                    **({"reason": reason} if reason else {}),
                },
                created_at=now,
            )
            session.add(event)
            session.flush()
            event.payload_json = {**event.payload_json, "event_id": event.event_id}
            session.add(
                OutboxMessageRow(
                    message_id=new_id("outbox"),
                    tenant_id=task.tenant_id,
                    kind="research.event.publish",
                    aggregate_id=str(event.event_id),
                    payload_json={"task_id": task.task_id, "event_id": event.event_id},
                    idempotency_key=f"event:{event.event_id}",
                    available_at=now,
                )
            )
