from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from decimal import Decimal
from threading import Lock, RLock
from typing import Any
import uuid

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from src.agent.schema.task import TaskBudget
from src.agent.schema.runtime import ExecutionContext
from src.agent.backends.persistence.database import SessionFactory
from src.agent.backends.persistence.models import NodeEffectRow, ResearchTaskRow


class BudgetDelta(BaseModel):
    model_config = ConfigDict(extra="forbid")

    search_calls: int = Field(default=0, ge=0)
    model_tokens: int = Field(default=0, ge=0)
    cost: Decimal = Field(default=Decimal("0"), ge=0)

    def is_zero(self) -> bool:
        return not (self.search_calls or self.model_tokens or self.cost)

    def add(self, other: "BudgetDelta") -> "BudgetDelta":
        return BudgetDelta(
            search_calls=self.search_calls + other.search_calls,
            model_tokens=self.model_tokens + other.model_tokens,
            cost=self.cost + other.cost,
        )


class BudgetExceededError(RuntimeError):
    def __init__(self, resource: str) -> None:
        super().__init__(f"Task budget is exhausted for {resource}.")
        self.resource = resource


@dataclass(frozen=True)
class EffectReservation:
    replay_payload: dict[str, Any] | None = None
    replay_kind: str | None = None
    recovered_pending: bool = False


@dataclass
class RuntimeEffectContext:
    execution: ExecutionContext
    node_run_id: str
    call_indexes: dict[str, int]

    def next_slot(self, namespace: str) -> int:
        value = self.call_indexes.get(namespace, 0)
        self.call_indexes[namespace] = value + 1
        return value


_runtime_effect_context: ContextVar[RuntimeEffectContext | None] = ContextVar(
    "deepresearch_runtime_effect_context", default=None
)


@contextmanager
def bind_runtime_effect_context(execution: ExecutionContext, node_run_id: str):
    token = _runtime_effect_context.set(
        RuntimeEffectContext(
            execution=execution,
            node_run_id=node_run_id,
            call_indexes={},
        )
    )
    try:
        yield
    finally:
        _runtime_effect_context.reset(token)


def current_runtime_effect_context() -> RuntimeEffectContext | None:
    return _runtime_effect_context.get()


class TaskBudgetManager:
    """Atomic reserve/settle using ResearchTask plus the existing NodeEffect row.

    Reservations are deliberately embedded in the semantic effect record.  This
    keeps TaskBudget as the only authoritative aggregate and avoids a second
    budget ledger while still making parallel reservations and replay safe.
    """

    _ENVELOPE_VERSION = 1

    def __init__(self, session_factory: SessionFactory) -> None:
        self.session_factory = session_factory
        self._locks_guard = Lock()
        self._locks: dict[str, RLock] = {}

    def _task_lock(self, task_id: str) -> RLock:
        with self._locks_guard:
            return self._locks.setdefault(task_id, RLock())

    def reserve_effect(
        self,
        *,
        tenant_id: str,
        task_id: str,
        node_run_id: str,
        effect_name: str,
        operation_key: str,
        upper_bound: BudgetDelta,
    ) -> EffectReservation:
        with self._task_lock(task_id), self.session_factory() as session, session.begin():
            existing = session.scalar(
                select(NodeEffectRow)
                .where(NodeEffectRow.operation_key == operation_key)
                .with_for_update()
            )
            if existing is not None:
                payload = dict(existing.result_json)
                if payload.get("_runtime_effect") == self._ENVELOPE_VERSION:
                    result = payload.get("result")
                    if isinstance(result, dict):
                        return EffectReservation(
                            replay_payload=dict(result.get("payload") or {}),
                            replay_kind=str(result.get("kind") or "unknown"),
                        )
                    return EffectReservation(recovered_pending=True)
                if payload.get("reservation") == "pending":
                    return EffectReservation(recovered_pending=True)
                return EffectReservation(replay_payload=payload, replay_kind="legacy")

            task = session.scalar(
                select(ResearchTaskRow)
                .where(
                    ResearchTaskRow.tenant_id == tenant_id,
                    ResearchTaskRow.task_id == task_id,
                )
                .with_for_update()
            )
            if task is None:
                raise LookupError(f"ResearchTask {task_id} was not found")
            budget = TaskBudget.model_validate(task.task_budget)
            outstanding = self._outstanding(session, task_id)
            projected = outstanding.add(upper_bound)
            if budget.usage.search_calls + projected.search_calls > budget.budget.max_search_calls:
                raise BudgetExceededError("search_calls")
            if budget.usage.model_tokens + projected.model_tokens > budget.budget.max_model_tokens:
                raise BudgetExceededError("model_tokens")
            if budget.usage.cost + projected.cost > budget.budget.max_cost:
                raise BudgetExceededError("cost")

            session.add(
                NodeEffectRow(
                    effect_id=f"effect_{uuid.uuid4().hex}",
                    tenant_id=tenant_id,
                    task_id=task_id,
                    node_run_id=node_run_id,
                    effect_name=effect_name,
                    operation_key=operation_key,
                    result_json=self._envelope(upper_bound),
                )
            )
            return EffectReservation()

    def settle_effect(
        self,
        *,
        task_id: str,
        operation_key: str,
        actual_usage: BudgetDelta,
        result_kind: str,
        result_payload: dict[str, Any],
    ) -> dict[str, Any]:
        with self._task_lock(task_id), self.session_factory() as session, session.begin():
            row = session.scalar(
                select(NodeEffectRow)
                .where(NodeEffectRow.operation_key == operation_key)
                .with_for_update()
            )
            if row is None:
                raise LookupError(f"NodeEffect {operation_key} was not reserved")
            envelope = dict(row.result_json)
            if envelope.get("_runtime_effect") == self._ENVELOPE_VERSION:
                result = envelope.get("result")
                if isinstance(result, dict):
                    return dict(result.get("payload") or {})
                reserved = BudgetDelta.model_validate(
                    dict(envelope.get("budget") or {}).get("reserved") or {}
                )
            else:
                reserved = actual_usage
            if (
                actual_usage.search_calls > reserved.search_calls
                or actual_usage.model_tokens > reserved.model_tokens
                or actual_usage.cost > reserved.cost
            ):
                raise ValueError("Actual budget usage exceeds its reservation")

            task = session.scalar(
                select(ResearchTaskRow)
                .where(ResearchTaskRow.task_id == task_id)
                .with_for_update()
            )
            if task is None:
                raise LookupError(f"ResearchTask {task_id} was not found")
            budget = TaskBudget.model_validate(task.task_budget)
            budget.usage.search_calls += actual_usage.search_calls
            budget.usage.model_tokens += actual_usage.model_tokens
            budget.usage.cost += actual_usage.cost
            task.task_budget = budget.model_dump(mode="json")
            row.result_json = self._envelope(
                reserved,
                actual=actual_usage,
                result_kind=result_kind,
                result_payload=result_payload,
            )
            return result_payload

    @classmethod
    def _outstanding(cls, session: Any, task_id: str) -> BudgetDelta:
        total = BudgetDelta()
        payloads = session.scalars(
            select(NodeEffectRow.result_json).where(NodeEffectRow.task_id == task_id)
        ).all()
        for payload in payloads:
            if payload.get("_runtime_effect") != cls._ENVELOPE_VERSION:
                continue
            budget = dict(payload.get("budget") or {})
            if budget.get("status") != "reserved":
                continue
            total = total.add(BudgetDelta.model_validate(budget.get("reserved") or {}))
        return total

    @classmethod
    def _envelope(
        cls,
        reserved: BudgetDelta,
        *,
        actual: BudgetDelta | None = None,
        result_kind: str | None = None,
        result_payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "_runtime_effect": cls._ENVELOPE_VERSION,
            "budget": {
                "status": "settled" if actual is not None else "reserved",
                "reserved": reserved.model_dump(mode="json"),
                "actual": (actual or BudgetDelta()).model_dump(mode="json"),
            },
        }
        if result_kind is not None and result_payload is not None:
            payload["result"] = {"kind": result_kind, "payload": result_payload}
        return payload
