from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class WriteModel(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)


class FilterPredicate(WriteModel):
    column: str = Field(min_length=1)
    op: Literal["=", "!=", ">", ">=", "<", "<=", "IN"]
    value: Any

    @model_validator(mode="after")
    def validate_in(self) -> "FilterPredicate":
        if self.op == "IN" and (not isinstance(self.value, list) or not self.value):
            raise ValueError("IN requires a non-empty list value")
        if self.value is None or (
            self.op == "IN" and any(item is None for item in self.value)
        ):
            raise ValueError("null filters require an explicit supported null operator")
        return self


class WritePlan(WriteModel):
    plan_id: str = Field(min_length=1)
    datasource_id: str = Field(min_length=1)
    operation: Literal["insert", "update", "delete"]
    table: str = Field(min_length=1)
    values: dict[str, Any] | None = None
    filters: list[FilterPredicate] | None = None
    reason: str = Field(min_length=1)
    preconditions: dict[str, Any] | None = None

    @model_validator(mode="after")
    def validate_operation_shape(self) -> "WritePlan":
        if self.operation == "insert" and not self.values:
            raise ValueError("insert requires values")
        if self.operation == "insert" and (self.filters or self.preconditions):
            raise ValueError("insert does not accept filters or preconditions")
        if self.operation == "update" and (not self.values or not self.filters):
            raise ValueError("update requires values and filters")
        if self.operation == "delete" and not self.filters:
            raise ValueError("delete requires filters")
        if self.operation == "delete" and self.values is not None:
            raise ValueError("delete does not accept values")
        return self


class WritePreview(WriteModel):
    datasource: str
    operation: Literal["insert", "update", "delete"]
    table: str
    filters: list[FilterPredicate] | None = None
    values: dict[str, Any] | None = None
    reason: str
    current_version: Any | None = None
    expected_value: Any | None = None


class WriteDecision(WriteModel):
    action: Literal["approved", "rejected"]
    actor_id: str
    at: datetime
    comment: str | None = None
    reason: str | None = None


class WriteResult(WriteModel):
    status: Literal["executed", "failed", "stale"]
    affected_rows: int = Field(default=0, ge=0)
    operation_key: str
    error_code: str | None = None
    committed_at: datetime | None = None
    replayed: bool = False


class PendingWrite(WriteModel):
    write_id: str
    task_id: str
    tenant_id: str
    user_id: str
    plan: WritePlan
    plan_hash: str
    preview: WritePreview
    policy_revision: str
    status: Literal[
        "pending",
        "approved",
        "executing",
        "rejected",
        "executed",
        "failed",
        "expired",
        "stale",
    ]
    created_at: datetime
    expires_at: datetime | None = None
    decision: WriteDecision | None = None
    operation_key: str
    result: WriteResult | None = None


class ApproveWriteRequest(WriteModel):
    plan_hash: str = Field(min_length=64, max_length=64)
    comment: str | None = Field(default=None, max_length=2000)


class RejectWriteRequest(WriteModel):
    reason: str | None = Field(default=None, max_length=2000)
