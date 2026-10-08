from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, model_validator

from src.agent.identity import canonical_json, sha256_hex
from src.agent.schema.model_config import FrozenModelBinding, ModelBindingSnapshot
from src.agent.evaluation.tool_flywheel import OfflineToolEvaluator, ToolDecisionPrediction
from src.agent.runtime.tool_decisions import ToolDecisionLabel


class ShadowDecisionModel(Protocol):
    def __call__(self, context: dict[str, Any]) -> ToolDecisionPrediction: ...


class ToolShadowResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    context_hash: str
    production: ToolDecisionPrediction
    candidate: ToolDecisionPrediction
    disagreement: bool
    candidate_selection_correct: bool
    candidate_arguments_correct: bool
    candidate_one_shot_success: bool
    permission_violation: bool


class ToolShadowOrchestrator:
    """Candidate inference only. This object has no ToolRuntime execution dependency."""

    def compare(
        self,
        *,
        context: dict[str, Any],
        sample_metadata: dict[str, Any],
        production: ToolDecisionPrediction,
        label: ToolDecisionLabel,
        candidate_model: ShadowDecisionModel,
    ) -> ToolShadowResult:
        candidate = candidate_model(context)
        if candidate.execution_status is not None:
            raise ValueError("Shadow candidate must never execute a Tool")
        synthetic_sample = {
            "sample_id": sample_metadata["sample_id"],
            "agent": sample_metadata["agent"],
            "difficulty": sample_metadata["difficulty"],
            "input": context,
            "label": label.model_dump(mode="json"),
        }
        summary = OfflineToolEvaluator().evaluate(
            [synthetic_sample], lambda _context: candidate
        )
        case = summary.cases[0]
        return ToolShadowResult(
            context_hash=sha256_hex(canonical_json(context)),
            production=production,
            candidate=candidate,
            disagreement=(
                production.selected_tool != candidate.selected_tool
                or production.arguments != candidate.arguments
            ),
            candidate_selection_correct=case.selection_correct,
            candidate_arguments_correct=case.arguments_correct,
            candidate_one_shot_success=case.one_shot_success,
            permission_violation=case.permission_violation,
        )


class ToolCanaryPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: str = Field(min_length=1)
    enabled: bool = False
    rollback: bool = False
    rollout_percent: int = Field(default=0, ge=0, le=100)
    assignment_salt: str = Field(min_length=1)
    eligible_tenants: list[str] = Field(default_factory=list)
    candidate_bindings: dict[str, FrozenModelBinding] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_bindings(self) -> "ToolCanaryPolicy":
        allowed = {"tool", "data", "code"}
        if set(self.candidate_bindings) - allowed:
            raise ValueError("Canary may only replace execution-agent Tool bindings")
        for key, binding in self.candidate_bindings.items():
            if binding.binding_key != key or binding.purpose != "chat":
                raise ValueError("Candidate binding identity does not match its binding key")
        if self.enabled and not self.rollback and not self.candidate_bindings:
            raise ValueError("Enabled Tool Canary requires candidate bindings")
        return self


class CanaryDiagnostics(BaseModel):
    model_config = ConfigDict(extra="forbid")
    policy_version: str
    assignment: Literal["baseline", "candidate"]
    reason: str
    bucket: int | None = None
    replaced_bindings: list[str] = Field(default_factory=list)


@dataclass(frozen=True)
class CanaryAssignment:
    snapshot: ModelBindingSnapshot
    diagnostics: CanaryDiagnostics


class ToolCanaryAssigner:
    def __init__(self, policy: ToolCanaryPolicy) -> None:
        self.policy = policy

    def assign(
        self,
        *,
        tenant_id: str,
        assignment_key: str,
        snapshot: ModelBindingSnapshot,
    ) -> CanaryAssignment:
        if not self.policy.enabled:
            return self._baseline(snapshot, "policy_disabled")
        if self.policy.rollback:
            return self._baseline(snapshot, "rollback_active")
        if self.policy.eligible_tenants and tenant_id not in self.policy.eligible_tenants:
            return self._baseline(snapshot, "tenant_not_eligible")
        bucket = int(
            sha256_hex(
                f"{self.policy.assignment_salt}:{self.policy.version}:"
                f"{tenant_id}:{assignment_key}"
            )[:8],
            16,
        ) % 100
        if bucket >= self.policy.rollout_percent:
            return CanaryAssignment(
                snapshot=snapshot,
                diagnostics=CanaryDiagnostics(
                    policy_version=self.policy.version,
                    assignment="baseline",
                    reason="outside_rollout_bucket",
                    bucket=bucket,
                ),
            )
        bindings = dict(snapshot.bindings)
        bindings.update(self.policy.candidate_bindings)  # type: ignore[arg-type]
        replaced = sorted(self.policy.candidate_bindings)
        return CanaryAssignment(
            snapshot=ModelBindingSnapshot(bindings=bindings),
            diagnostics=CanaryDiagnostics(
                policy_version=self.policy.version,
                assignment="candidate",
                reason="eligible_rollout_bucket",
                bucket=bucket,
                replaced_bindings=replaced,
            ),
        )

    def _baseline(
        self, snapshot: ModelBindingSnapshot, reason: str
    ) -> CanaryAssignment:
        return CanaryAssignment(
            snapshot=snapshot,
            diagnostics=CanaryDiagnostics(
                policy_version=self.policy.version,
                assignment="baseline",
                reason=reason,
            ),
        )
