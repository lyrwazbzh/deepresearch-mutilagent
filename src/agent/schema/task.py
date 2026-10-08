from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from src.agent.schema.capability import CapabilitySnapshot
from src.agent.schema.model_config import ModelBindingSnapshot
from src.agent.schema.state import ResearchState


class TaskStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    PAUSING = "pausing"
    PAUSED = "paused"
    CANCELLING = "cancelling"
    CANCELLED = "cancelled"
    COMPLETED = "completed"
    FAILED = "failed"


class BudgetLimits(BaseModel):
    model_config = ConfigDict(extra="forbid")
    max_wall_time_ms: int = Field(default=1_800_000, gt=0)
    max_search_calls: int = Field(default=60, ge=0)
    max_model_tokens: int = Field(default=300_000, ge=0)
    max_cost: Decimal = Field(default=Decimal("10.00"), ge=0)
    currency: str = "USD"


class BudgetUsage(BaseModel):
    model_config = ConfigDict(extra="forbid")
    wall_time_ms: int = Field(default=0, ge=0)
    search_calls: int = Field(default=0, ge=0)
    model_tokens: int = Field(default=0, ge=0)
    cost: Decimal = Field(default=Decimal("0"), ge=0)


class TaskBudget(BaseModel):
    model_config = ConfigDict(extra="forbid")
    budget: BudgetLimits = Field(default_factory=BudgetLimits)
    usage: BudgetUsage = Field(default_factory=BudgetUsage)

    def remaining_projection(self) -> dict[str, int | str]:
        return {
            "wall_time_ms": max(0, self.budget.max_wall_time_ms - self.usage.wall_time_ms),
            "search_calls": max(0, self.budget.max_search_calls - self.usage.search_calls),
            "model_tokens": max(0, self.budget.max_model_tokens - self.usage.model_tokens),
            "cost": str(max(Decimal("0"), self.budget.max_cost - self.usage.cost)),
            "currency": self.budget.currency,
        }


class ResearchTask(BaseModel):
    model_config = ConfigDict(extra="forbid")
    tenant_id: str
    user_id: str
    task_id: str
    thread_id: str
    status: TaskStatus
    latest_state: ResearchState
    model_binding_snapshot: ModelBindingSnapshot
    capability_snapshot: CapabilitySnapshot
    task_budget: TaskBudget
    lease_token: str | None = None
    leased_until: datetime | None = None
    budget_accounted_at: datetime | None = None
    created_at: datetime
    updated_at: datetime
