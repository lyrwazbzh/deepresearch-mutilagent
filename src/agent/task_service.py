from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from src.agent.schema.capability import CapabilitySnapshot
from src.agent.identity import canonical_json
from src.agent.schema.task import ResearchTask, TaskBudget, TaskStatus
from src.agent.schema.state import ExecutionState, ResearchState
from src.agent.schema.model_config import BindingKey
from src.agent.llm.runtime import PHASE1_BINDINGS, ModelConfigurationError, ModelResolver
from src.agent.evaluation.model_rollout import ToolCanaryAssigner
from src.agent.backends.persistence.database import SessionFactory
from src.agent.backends.persistence.models import (
    ConversationMessageRow,
    ExportJobRow,
    OutboxMessageRow,
    ResearchCheckpointRow,
    ResearchEventRow,
    ResearchTaskCommandRow,
    ResearchTaskRow,
    ShareRow,
    TaskKnowledgeGraphRow,
    ThreadRow,
    utcnow,
)
from src.agent.backends.persistence.repositories import (
    CheckpointRepository,
    MessageRepository,
    TaskRepository,
    ThreadRepository,
)
from src.agent.errors import (
    DependencyUnavailableError,
    IdempotencyConflictError,
    IdempotencyTargetDeletedError,
    InvalidStateError,
    NotFoundError,
)

NonBlank = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


class CreateTaskOptions(BaseModel):
    model_config = ConfigDict(extra="forbid")
    max_depth: int = 3
    max_revision_rounds: int = 2
    datasource_ids: list[str] = Field(default_factory=list)

    @field_validator("max_depth", "max_revision_rounds")
    @classmethod
    def clamp_limit(cls, value: int) -> int:
        if value < 0:
            raise ValueError("must be greater than or equal to zero")
        return min(value, 5)


class CreateTaskRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    query: NonBlank
    thread_id: str | None = None
    options: CreateTaskOptions = Field(default_factory=CreateTaskOptions)


class ReplanRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    instruction: NonBlank


class TaskProjection(BaseModel):
    model_config = ConfigDict(extra="forbid")
    task_id: str
    thread_id: str
    message_id: str | None = None
    task_status: TaskStatus
    route: dict[str, Any] | None = None
    pending_write: str | None = None
    phase: str
    report: dict[str, Any]
    updated_at: datetime


class CreatedTask(BaseModel):
    projection: TaskProjection
    replayed: bool


class QueryCompletionService:
    def __init__(self, *, message_limit: int = 12, token_budget: int = 4_000) -> None:
        self.message_limit = max(0, message_limit)
        self.token_budget = max(1, token_budget)

    def complete(self, prior: list[ConversationMessageRow], current_query: str) -> str:
        current_budget = self.token_budget * 4
        bounded_current = current_query[-current_budget:]
        remaining_tokens = max(0, self.token_budget - self._tokens(bounded_current))
        wrapper = "Conversation context:\n{context}\n\nCurrent request:\n{current}"
        wrapper_cost = self._tokens(wrapper.format(context="", current=""))
        if remaining_tokens <= wrapper_cost:
            return bounded_current
        remaining_tokens -= wrapper_cost
        selected: list[ConversationMessageRow] = []
        for message in reversed(prior[-self.message_limit :]):
            if message.role not in {"user", "assistant"}:
                continue
            cost = self._tokens(f"{message.role}: {message.content}")
            if cost > remaining_tokens:
                continue
            selected.append(message)
            remaining_tokens -= cost
        if not selected:
            return bounded_current
        selected.reverse()
        while selected:
            context = "\n".join(
                f"{message.role}: {message.content}" for message in selected
            )
            completed = wrapper.format(context=context, current=bounded_current)
            if self._tokens(completed) <= self.token_budget:
                return completed
            selected.pop(0)
        return bounded_current

    @staticmethod
    def _tokens(value: str) -> int:
        return max(1, (len(value) + 3) // 4)


class TaskService:
    def __init__(
        self,
        session_factory: SessionFactory,
        model_resolver: ModelResolver,
        query_completion: QueryCompletionService | None = None,
        required_bindings: tuple[BindingKey, ...] = PHASE1_BINDINGS,
        capability_snapshot_builder: Callable[[list[str]], CapabilitySnapshot] | None = None,
        phase9_enabled: bool = False,
        tool_canary_assigner: ToolCanaryAssigner | None = None,
    ) -> None:
        self.session_factory = session_factory
        self.model_resolver = model_resolver
        self.query_completion = query_completion or QueryCompletionService()
        self.required_bindings = required_bindings
        self.phase9_enabled = phase9_enabled
        self.tool_canary_assigner = tool_canary_assigner
        self.capability_snapshot_builder = capability_snapshot_builder or (
            lambda datasource_ids: CapabilitySnapshot(
                policy_revision="unconfigured",
                datasource_ids=datasource_ids,
            )
        )

    @staticmethod
    def request_hash(request: CreateTaskRequest) -> str:
        normalized = request.model_dump(mode="json", exclude_none=False)
        encoded = json.dumps(normalized, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    def create(
        self,
        tenant_id: str,
        user_id: str,
        idempotency_key: str,
        request: CreateTaskRequest,
    ) -> CreatedTask:
        request_hash = self.request_hash(request)
        try:
            with self.session_factory() as session, session.begin():
                tasks = TaskRepository(session, tenant_id, user_id)
                existing = tasks.by_idempotency_key(idempotency_key)
                if existing is not None:
                    return self._idempotent_result(existing, request_hash)

                thread_id = request.thread_id or new_id("thread")
                threads = ThreadRepository(session, tenant_id, user_id)
                thread = threads.get(thread_id)
                if thread is None:
                    if request.thread_id is not None:
                        raise NotFoundError("Thread was not found.")
                    thread = ThreadRow(
                        thread_id=thread_id,
                        tenant_id=tenant_id,
                        user_id=user_id,
                    )
                    threads.add(thread)

                messages = MessageRepository(session, tenant_id, user_id)
                prior = messages.recent(
                    thread_id, limit=self.query_completion.message_limit
                )
                task_id = new_id("task")
                message_id = new_id("msg")
                completed_query = self.query_completion.complete(prior, request.query)
                messages.add(
                    ConversationMessageRow(
                        message_id=message_id,
                        tenant_id=tenant_id,
                        user_id=user_id,
                        thread_id=thread_id,
                        task_id=task_id,
                        role="user",
                        content=request.query,
                    )
                )
                try:
                    snapshot = self.model_resolver.resolve_snapshot(
                        session, tenant_id, self.required_bindings
                    )
                    if self.tool_canary_assigner is not None:
                        snapshot = self.tool_canary_assigner.assign(
                            tenant_id=tenant_id,
                            assignment_key=f"{user_id}:{idempotency_key}",
                            snapshot=snapshot,
                        ).snapshot
                except ModelConfigurationError as exc:
                    raise DependencyUnavailableError(str(exc)) from exc

                now = utcnow()
                state = ResearchState(
                    tenant_id=tenant_id,
                    user_id=user_id,
                    thread_id=thread_id,
                    task_id=task_id,
                    query=completed_query,
                    execution=ExecutionState(
                        max_depth=request.options.max_depth,
                        max_revision_rounds=request.options.max_revision_rounds,
                    ),
                )
                budget = TaskBudget()
                task = ResearchTaskRow(
                    task_id=task_id,
                    tenant_id=tenant_id,
                    user_id=user_id,
                    thread_id=thread_id,
                    status=TaskStatus.PENDING.value,
                    latest_state=state.model_dump(mode="json"),
                    model_binding_snapshot=snapshot.model_dump(mode="json"),
                    capability_snapshot=self.capability_snapshot_builder(
                        request.options.datasource_ids
                    ).model_dump(mode="json"),
                    create_idempotency_key=idempotency_key,
                    create_request_hash=request_hash,
                    task_budget=budget.model_dump(mode="json"),
                    created_at=now,
                    updated_at=now,
                )
                tasks.add(task)
                # Materialize the aggregate root before inserting rows that carry a
                # database-level FK to it.  SQLite's default test configuration does
                # not enforce this ordering, while PostgreSQL does.
                session.flush()
                checkpoint_id = new_id("ckpt")
                session.add(
                    ResearchCheckpointRow(
                        checkpoint_id=checkpoint_id,
                        tenant_id=tenant_id,
                        task_id=task_id,
                        checkpoint_seq=0,
                        state_snapshot=state.model_dump(mode="json"),
                        graph_position="START",
                        scheduling_state={},
                    )
                )
                session.add(
                    OutboxMessageRow(
                        message_id=new_id("outbox"),
                        tenant_id=tenant_id,
                        kind="research.schedule",
                        aggregate_id=task_id,
                        payload_json={"task_id": task_id},
                        idempotency_key=f"create:{task_id}:schedule",
                        available_at=now,
                    )
                )
                session.flush()
                projection = self._projection(task, state, "START", message_id=message_id)
                return CreatedTask(projection=projection, replayed=False)
        except IntegrityError:
            # The unique key is the concurrency arbiter. All losing transaction writes roll back.
            with self.session_factory() as session:
                existing = TaskRepository(session, tenant_id, user_id).by_idempotency_key(
                    idempotency_key
                )
                if existing is None:
                    raise
                return self._idempotent_result(existing, request_hash)

    def _idempotent_result(self, row: ResearchTaskRow, request_hash: str) -> CreatedTask:
        if row.create_request_hash != request_hash:
            raise IdempotencyConflictError("Idempotency-Key was used with a different request.")
        if row.deleted_at is not None:
            raise IdempotencyTargetDeletedError("The original task was deleted.")
        state = ResearchState.model_validate(row.latest_state)
        return CreatedTask(
            projection=self._projection(
                row,
                state,
                self._latest_position(row.task_id),
                message_id=self._message_id(row.task_id),
            ),
            replayed=True,
        )

    def _message_id(self, task_id: str) -> str | None:
        with self.session_factory() as session:
            return session.scalar(
                select(ConversationMessageRow.message_id)
                .where(
                    ConversationMessageRow.task_id == task_id,
                    ConversationMessageRow.role == "user",
                )
                .order_by(ConversationMessageRow.created_at)
                .limit(1)
            )

    def _latest_position(self, task_id: str) -> str:
        from sqlalchemy import select

        with self.session_factory() as session:
            checkpoint = session.scalar(
                select(ResearchCheckpointRow)
                .where(ResearchCheckpointRow.task_id == task_id)
                .order_by(ResearchCheckpointRow.checkpoint_seq.desc())
                .limit(1)
            )
            return checkpoint.graph_position if checkpoint else "START"

    def get(self, tenant_id: str, user_id: str, task_id: str) -> TaskProjection:
        from sqlalchemy import select

        with self.session_factory() as session:
            task = TaskRepository(session, tenant_id, user_id).get(task_id)
            if task is None or task.deleted_at is not None:
                raise NotFoundError("Task was not found.")
            checkpoint = session.scalar(
                select(ResearchCheckpointRow)
                .where(
                    ResearchCheckpointRow.tenant_id == tenant_id,
                    ResearchCheckpointRow.task_id == task_id,
                )
                .order_by(ResearchCheckpointRow.checkpoint_seq.desc())
                .limit(1)
            )
            return self._projection(
                task,
                ResearchState.model_validate(task.latest_state),
                checkpoint.graph_position if checkpoint else "START",
            )

    def delete(self, tenant_id: str, user_id: str, task_id: str) -> None:
        with self.session_factory() as session, session.begin():
            task = TaskRepository(session, tenant_id, user_id).get(task_id, for_update=True)
            if task is None or task.deleted_at is not None:
                raise NotFoundError("Task was not found.")
            if task.status not in {
                TaskStatus.CANCELLED.value,
                TaskStatus.COMPLETED.value,
                TaskStatus.FAILED.value,
            }:
                raise InvalidStateError("Only a terminal task can be deleted.")
            now = utcnow()
            task.deleted_at = now
            task.updated_at = now
            shares = session.scalars(
                select(ShareRow).where(
                    ShareRow.tenant_id == tenant_id,
                    ShareRow.task_id == task_id,
                    ShareRow.revoked_at.is_(None),
                )
            ).all()
            for share in shares:
                share.revoked_at = now
            exports = session.scalars(
                select(ExportJobRow).where(
                    ExportJobRow.tenant_id == tenant_id,
                    ExportJobRow.task_id == task_id,
                    ExportJobRow.status.in_({"pending", "running"}),
                )
            ).all()
            for export in exports:
                export.status = "failed"
                export.error_code = "task_deleted"
                export.updated_at = now
                event = ResearchEventRow(
                    tenant_id=tenant_id,
                    task_id=task_id,
                    event_type="export_updated",
                    payload_json={
                        "event_id": 0,
                        "task_id": task_id,
                        "export_id": export.export_id,
                        "status": "failed",
                        "at": now.isoformat(),
                    },
                    created_at=now,
                )
                session.add(event)
                session.flush()
                event.payload_json = {**event.payload_json, "event_id": event.event_id}
                session.add(
                    OutboxMessageRow(
                        message_id=new_id("outbox"),
                        tenant_id=tenant_id,
                        kind="research.event.publish",
                        aggregate_id=str(event.event_id),
                        payload_json={"task_id": task_id, "event_id": event.event_id},
                        idempotency_key=f"event:{event.event_id}",
                        available_at=now,
                    )
                )

    def command(
        self,
        tenant_id: str,
        user_id: str,
        task_id: str,
        command_type: str,
        idempotency_key: str,
    ) -> TaskProjection:
        try:
            return self._command_once(
                tenant_id,
                user_id,
                task_id,
                command_type,
                idempotency_key,
            )
        except IntegrityError:
            request_hash = hashlib.sha256(
                f"{tenant_id}|{user_id}|{task_id}|{command_type}".encode()
            ).hexdigest()
            with self.session_factory() as session:
                existing = session.scalar(
                    select(ResearchTaskCommandRow).where(
                        ResearchTaskCommandRow.tenant_id == tenant_id,
                        ResearchTaskCommandRow.user_id == user_id,
                        ResearchTaskCommandRow.task_id == task_id,
                        ResearchTaskCommandRow.idempotency_key == idempotency_key,
                    )
                )
                if existing is None:
                    raise
                if existing.request_hash != request_hash:
                    raise IdempotencyConflictError(
                        "Idempotency-Key was used with a different task command."
                    )
                return TaskProjection.model_validate(existing.response_json)

    def _command_once(
        self,
        tenant_id: str,
        user_id: str,
        task_id: str,
        command_type: str,
        idempotency_key: str,
    ) -> TaskProjection:
        if command_type not in {"pause", "resume", "cancel"}:
            raise InvalidStateError(f"Unsupported task command: {command_type}")
        request_hash = hashlib.sha256(
            f"{tenant_id}|{user_id}|{task_id}|{command_type}".encode()
        ).hexdigest()
        with self.session_factory() as session, session.begin():
            existing = session.scalar(
                select(ResearchTaskCommandRow).where(
                    ResearchTaskCommandRow.tenant_id == tenant_id,
                    ResearchTaskCommandRow.user_id == user_id,
                    ResearchTaskCommandRow.task_id == task_id,
                    ResearchTaskCommandRow.idempotency_key == idempotency_key,
                )
            )
            if existing is not None:
                if existing.request_hash != request_hash:
                    raise IdempotencyConflictError(
                        "Idempotency-Key was used with a different task command."
                    )
                return TaskProjection.model_validate(existing.response_json)

            task = TaskRepository(session, tenant_id, user_id).get(task_id, for_update=True)
            if task is None or task.deleted_at is not None:
                raise NotFoundError("Task was not found.")
            before = task.status
            if command_type == "pause":
                if before == TaskStatus.PENDING.value:
                    task.status = TaskStatus.PAUSED.value
                elif before == TaskStatus.RUNNING.value:
                    task.status = TaskStatus.PAUSING.value
                elif before not in {TaskStatus.PAUSING.value, TaskStatus.PAUSED.value}:
                    raise InvalidStateError(f"Task status {before} cannot be paused.")
            elif command_type == "resume":
                if before != TaskStatus.PAUSED.value:
                    raise InvalidStateError(f"Task status {before} cannot be resumed.")
                current_state = ResearchState.model_validate(task.latest_state)
                if current_state.pending_write is not None:
                    raise InvalidStateError(
                        "A pending database write must be approved or rejected before resume."
                    )
                task.status = TaskStatus.PENDING.value
            else:
                if before in {TaskStatus.PENDING.value, TaskStatus.PAUSED.value}:
                    task.status = TaskStatus.CANCELLED.value
                elif before in {TaskStatus.RUNNING.value, TaskStatus.PAUSING.value}:
                    task.status = TaskStatus.CANCELLING.value
                elif before != TaskStatus.CANCELLED.value:
                    raise InvalidStateError(f"Task status {before} cannot be cancelled.")

            now = utcnow()
            task.updated_at = now
            checkpoint = session.scalar(
                select(ResearchCheckpointRow)
                .where(ResearchCheckpointRow.task_id == task_id)
                .order_by(ResearchCheckpointRow.checkpoint_seq.desc())
                .limit(1)
            )
            position = checkpoint.graph_position if checkpoint else "START"
            projection = self._projection(
                task, ResearchState.model_validate(task.latest_state), position
            )
            event = ResearchEventRow(
                tenant_id=tenant_id,
                task_id=task_id,
                event_type="task_updated",
                payload_json={
                    "event_id": 0,
                    "task_id": task_id,
                    "changed": ["task_status"],
                    "at": now.isoformat(),
                    "command": command_type,
                },
                created_at=now,
            )
            session.add(event)
            session.flush()
            event.payload_json = {**event.payload_json, "event_id": event.event_id}
            outbox = [
                OutboxMessageRow(
                    message_id=new_id("outbox"),
                    tenant_id=tenant_id,
                    kind="research.event.publish",
                    aggregate_id=str(event.event_id),
                    payload_json={"task_id": task_id, "event_id": event.event_id},
                    idempotency_key=f"event:{event.event_id}",
                    available_at=now,
                )
            ]
            if command_type == "resume":
                outbox.append(
                    OutboxMessageRow(
                        message_id=new_id("outbox"),
                        tenant_id=tenant_id,
                        kind="research.schedule",
                        aggregate_id=task_id,
                        payload_json={"task_id": task_id},
                        idempotency_key=f"resume:{task_id}:{idempotency_key}:schedule",
                        available_at=now,
                    )
                )
            session.add_all(outbox)
            session.add(
                ResearchTaskCommandRow(
                    tenant_id=tenant_id,
                    user_id=user_id,
                    task_id=task_id,
                    idempotency_key=idempotency_key,
                    command_type=command_type,
                    request_hash=request_hash,
                    accepted_http_status=202,
                    response_json=projection.model_dump(mode="json"),
                    created_at=now,
                )
            )
            return projection

    def replan(
        self,
        tenant_id: str,
        user_id: str,
        task_id: str,
        idempotency_key: str,
        request: ReplanRequest,
    ) -> TaskProjection:
        request_hash = hashlib.sha256(
            canonical_json(
                {
                    "command_type": "replan",
                    "task_id": task_id,
                    "body": request.model_dump(mode="json"),
                }
            )
        ).hexdigest()
        message_id = "msg_" + hashlib.sha256(
            f"{tenant_id}|{user_id}|{task_id}|replan|{idempotency_key}".encode()
        ).hexdigest()[:32]
        with self.session_factory() as session, session.begin():
            tasks = TaskRepository(session, tenant_id, user_id)
            task = tasks.get(task_id, for_update=True)
            if task is None or task.deleted_at is not None:
                raise NotFoundError("Task was not found.")
            command = session.scalar(
                select(ResearchTaskCommandRow).where(
                    ResearchTaskCommandRow.tenant_id == tenant_id,
                    ResearchTaskCommandRow.user_id == user_id,
                    ResearchTaskCommandRow.task_id == task_id,
                    ResearchTaskCommandRow.idempotency_key == idempotency_key,
                )
            )
            if command is not None:
                if command.request_hash != request_hash:
                    raise IdempotencyConflictError(
                        "Idempotency-Key was used with a different task command."
                    )
                return TaskProjection.model_validate(command.response_json)
            existing_message = session.scalar(
                select(ConversationMessageRow).where(
                    ConversationMessageRow.message_id == message_id,
                    ConversationMessageRow.tenant_id == tenant_id,
                    ConversationMessageRow.user_id == user_id,
                )
            )
            if existing_message is not None:
                if existing_message.content != request.instruction:
                    raise IdempotencyConflictError(
                        "Idempotency-Key was used with a different replan instruction."
                    )
                projection = self._projection(
                    task,
                    ResearchState.model_validate(task.latest_state),
                    "START",
                    message_id=message_id,
                )
                session.add(
                    ResearchTaskCommandRow(
                        tenant_id=tenant_id,
                        user_id=user_id,
                        task_id=task_id,
                        idempotency_key=idempotency_key,
                        command_type="replan",
                        request_hash=request_hash,
                        accepted_http_status=202,
                        response_json=projection.model_dump(mode="json"),
                        created_at=utcnow(),
                    )
                )
                return projection
            if task.status != TaskStatus.RUNNING.value:
                raise InvalidStateError("Only a running task can be replanned.")
            messages = MessageRepository(session, tenant_id, user_id)
            prior = messages.recent(
                task.thread_id, limit=self.query_completion.message_limit
            )
            completed_query = self.query_completion.complete(prior, request.instruction)
            messages.add(
                ConversationMessageRow(
                    message_id=message_id,
                    tenant_id=tenant_id,
                    user_id=user_id,
                    thread_id=task.thread_id,
                    task_id=task.task_id,
                    role="user",
                    content=request.instruction,
                )
            )
            state = ResearchState.model_validate(task.latest_state)
            state.query = completed_query
            state.plan = None
            state.route = None
            state.memory_context = None
            state.analysis_requests = []
            state.coverage = None
            state.critic = None
            state.execution.depth = 0
            now = utcnow()
            state.timestamps.updated_at = now
            task.latest_state = state.model_dump(mode="json")
            if self.phase9_enabled:
                query_hash = hashlib.sha256(
                    " ".join(state.query.casefold().split()).encode()
                ).hexdigest()
                plan_hash = hashlib.sha256(canonical_json([])).hexdigest()
                metadata = session.get(TaskKnowledgeGraphRow, (tenant_id, task_id))
                if metadata is None:
                    metadata = TaskKnowledgeGraphRow(
                        tenant_id=tenant_id,
                        task_id=task_id,
                        status="building",
                        query_hash=query_hash,
                        plan_hash=plan_hash,
                        processed_evidence_count=0,
                        updated_at=now,
                    )
                    session.add(metadata)
                else:
                    metadata.status = "building"
                    metadata.query_hash = query_hash
                    metadata.plan_hash = plan_hash
                    metadata.processed_evidence_count = 0
                    metadata.updated_at = now
            # Fence the prior activation and schedule from the last committed checkpoint.
            task.status = TaskStatus.PENDING.value
            task.lease_token = None
            task.leased_until = None
            task.budget_accounted_at = None
            task.updated_at = now
            checkpoint = ResearchCheckpointRow(
                checkpoint_id=new_id("ckpt"),
                tenant_id=tenant_id,
                task_id=task_id,
                checkpoint_seq=CheckpointRepository(session, tenant_id, task_id).next_seq(),
                state_snapshot=task.latest_state,
                graph_position="START",
                scheduling_state={"replan_message_id": message_id},
            )
            session.add(checkpoint)
            event = ResearchEventRow(
                tenant_id=tenant_id,
                task_id=task_id,
                event_type="task_updated",
                payload_json={
                    "event_id": 0,
                    "task_id": task_id,
                    "changed": ["messages", "route", "phase", "task_status"],
                    "at": now.isoformat(),
                },
                created_at=now,
            )
            session.add(event)
            session.flush()
            event.payload_json = {**event.payload_json, "event_id": event.event_id}
            outbox_rows = [
                    OutboxMessageRow(
                        message_id=new_id("outbox"),
                        tenant_id=tenant_id,
                        kind="research.schedule",
                        aggregate_id=task_id,
                        payload_json={"task_id": task_id},
                        idempotency_key=f"replan:{task_id}:{message_id}:schedule",
                        available_at=now,
                    ),
                    OutboxMessageRow(
                        message_id=new_id("outbox"),
                        tenant_id=tenant_id,
                        kind="research.event.publish",
                        aggregate_id=str(event.event_id),
                        payload_json={"task_id": task_id, "event_id": event.event_id},
                        idempotency_key=f"event:{event.event_id}",
                        available_at=now,
                    ),
                ]
            if self.phase9_enabled:
                outbox_rows.append(
                    OutboxMessageRow(
                        message_id=new_id("outbox"),
                        tenant_id=tenant_id,
                        kind="knowledge_graph.build",
                        aggregate_id=task_id,
                        payload_json={"task_id": task_id},
                        idempotency_key=f"kg:replan:{task_id}:{message_id}",
                        available_at=now,
                    )
                )
            session.add_all(outbox_rows)
            projection = self._projection(task, state, "START", message_id=message_id)
            session.add(
                ResearchTaskCommandRow(
                    tenant_id=tenant_id,
                    user_id=user_id,
                    task_id=task_id,
                    idempotency_key=idempotency_key,
                    command_type="replan",
                    request_hash=request_hash,
                    accepted_http_status=202,
                    response_json=projection.model_dump(mode="json"),
                    created_at=now,
                )
            )
            return projection

    @staticmethod
    def _projection(
        row: ResearchTaskRow,
        state: ResearchState,
        graph_position: str,
        *,
        message_id: str | None = None,
    ) -> TaskProjection:
        phase_by_position = {
            "START": "routing",
            "route": "routing",
            "focused_dispatch": "analyzing",
            "deep_scout": "analyzing",
            "data_analyst": "analyzing",
            "code_wizard": "analyzing",
            "chief_direct": "completed" if row.status == TaskStatus.COMPLETED.value else "writing",
            "chief_plan": "planning",
            "retrieval_dispatch": "retrieving",
            "deep_scout_web": "retrieving",
            "deep_scout_rag": "retrieving",
            "data_analyst_sql": "retrieving",
            "retrieval_join": "retrieving",
            "merge_evidence": "analyzing",
            "database_write_gate": "reviewing",
            "prepare_write": "reviewing",
            "write_rejected": "writing",
            "write_terminal": "writing",
            "write_stale": "reviewing",
            "lead_writer": "writing",
            "validate_report": "writing",
            "revise": "writing",
            "finalize": "writing",
            "graph_success": "writing",
        }
        return TaskProjection(
            task_id=row.task_id,
            thread_id=row.thread_id,
            message_id=message_id,
            task_status=TaskStatus(row.status),
            route=state.route.model_dump(mode="json") if state.route else None,
            pending_write=state.pending_write,
            phase="completed"
            if row.status == TaskStatus.COMPLETED.value
            else phase_by_position.get(graph_position, "routing"),
            report=state.report.model_dump(mode="json", exclude_none=True),
            updated_at=(
                row.updated_at
                if row.updated_at.tzinfo is not None
                else row.updated_at.replace(tzinfo=timezone.utc)
            ),
        )

    @staticmethod
    def to_domain(row: ResearchTaskRow) -> ResearchTask:
        return ResearchTask.model_validate(
            {
                "tenant_id": row.tenant_id,
                "user_id": row.user_id,
                "task_id": row.task_id,
                "thread_id": row.thread_id,
                "status": row.status,
                "latest_state": row.latest_state,
                "model_binding_snapshot": row.model_binding_snapshot,
                "capability_snapshot": row.capability_snapshot,
                "task_budget": row.task_budget,
                "lease_token": row.lease_token,
                "leased_until": row.leased_until,
                "budget_accounted_at": row.budget_accounted_at,
                "created_at": row.created_at,
                "updated_at": row.updated_at,
            }
        )
