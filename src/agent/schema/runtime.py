from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, model_validator


class CancellationToken(Protocol):
    def is_cancelled(self) -> bool: ...


@dataclass(frozen=True)
class ExecutionContext:
    tenant_id: str
    user_id: str
    task_id: str
    thread_id: str
    lease_token: str
    cancellation_token: CancellationToken
    deadline: datetime
    capability_snapshot: dict[str, Any] | None = None


class ContextPacket(BaseModel):
    model_config = ConfigDict(extra="forbid")
    role: Literal[
        "route",
        "chief_direct",
        "chief_plan",
        "chief_replan",
        "deep_scout",
        "data_analyst",
        "code_wizard",
        "deep_scout_web",
        "deep_scout_rag",
        "data_analyst_sql",
        "analysis_decision",
        "code_wizard_compute",
        "coverage",
        "chief_deepen",
        "critic",
        "phase3_boundary",
        "database_write_gate",
        "prepare_write",
        "lead_writer",
        "validate_report",
        "revise",
        "finalize",
        "graph_success",
        "graph_failure",
    ]
    task_id: str
    query: str
    deadline: datetime
    remaining_budget: dict[str, Any] | None = None
    skills: list[dict[str, Any]] = Field(default_factory=list)
    payload: dict[str, Any] = Field(default_factory=dict)


class StatePatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    values: dict[str, Any] = Field(default_factory=dict)
    list_appends: dict[str, list[Any]] = Field(default_factory=dict)
    branch_updates: dict[str, dict[str, Any]] = Field(default_factory=dict)
    analysis_request_appends: list[dict[str, Any]] = Field(default_factory=list)
    analysis_status_updates: dict[str, Literal["completed", "failed"]] = Field(
        default_factory=dict
    )

    @model_validator(mode="after")
    def disjoint_operations(self) -> "StatePatch":
        overlap = set(self.values) & set(self.list_appends)
        if overlap:
            raise ValueError(f"fields cannot be replaced and appended together: {sorted(overlap)}")
        if self.branch_updates and "research_rounds" in self.values:
            raise ValueError("research_rounds cannot be replaced and branch-updated together")
        if (self.analysis_request_appends or self.analysis_status_updates) and (
            "analysis_requests" in self.values or "analysis_requests" in self.list_appends
        ):
            raise ValueError("analysis_requests must use dedicated patch operations")
        return self


class AssistantMessageEffect(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["assistant_message"] = "assistant_message"
    content: str


class PendingWriteEffect(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["pending_write"] = "pending_write"
    pending_write: dict[str, Any]


class NodeResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    patch: StatePatch = Field(default_factory=StatePatch)
    effects: list[AssistantMessageEffect | PendingWriteEffect] = Field(default_factory=list)
    output: dict[str, Any] = Field(default_factory=dict)


@dataclass(frozen=True)
class NodeExecution:
    node_run_id: str
    node_attempt_no: int
    replayed: bool
    result: NodeResult
    checkpoint_id: str | None
    state: dict[str, Any]


Failpoint = Callable[[str], None]
