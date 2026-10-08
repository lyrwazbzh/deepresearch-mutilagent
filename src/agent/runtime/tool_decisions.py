from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from src.agent.backends.audit import AuditService
from src.agent.schema.capability import CapabilitySnapshot
from src.agent.identity import canonical_json, sha256_hex
from src.agent.backends.persistence.database import SessionFactory
from src.agent.backends.persistence.models import (
    DecisionTraceRow,
    ResearchTaskRow,
    ToolDecisionReviewRow,
    utcnow,
)
from src.agent.runtime.tool_runtime import ToolRegistry, ToolResult


EXECUTION_AGENTS = frozenset({"deep_scout", "data_analyst", "code_wizard"})
NO_TOOL = "NO_TOOL"
LabelSource = Literal[
    "human_gold", "curated_eval", "deterministic_rule", "validated_outcome"
]
Confidence = Literal["high", "medium", "low"]
Difficulty = Literal["trivial", "confusable", "compositional"]


class AvailableTool(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1)
    schema_hash: str = Field(min_length=64, max_length=64)


class ToolDecisionContext(BaseModel):
    model_config = ConfigDict(extra="forbid")
    current_intent: str = Field(min_length=1, max_length=8_000)
    toolset_snapshot_ref: str = Field(min_length=64, max_length=64)
    available_tools: list[AvailableTool]
    prior_tool_results: list[dict[str, Any]] = Field(default_factory=list)


class ToolDecisionValue(BaseModel):
    model_config = ConfigDict(extra="forbid")
    selected_tool: str = Field(min_length=1)
    arguments: dict[str, Any] | None = None

    @model_validator(mode="after")
    def shape(self) -> "ToolDecisionValue":
        if self.selected_tool == NO_TOOL and self.arguments:
            raise ValueError("NO_TOOL cannot carry arguments")
        if self.selected_tool != NO_TOOL and self.arguments is None:
            raise ValueError("Tool Call requires arguments")
        return self


class ToolDecisionExecution(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: Literal["success", "error"]
    normalized_error: str | None = None
    retry_count: int = Field(default=0, ge=0)
    is_fallback: bool = False


class ToolDecisionOutcome(BaseModel):
    model_config = ConfigDict(extra="allow")
    result_accepted: bool | None = None
    evidence_created: bool | None = None


class ArgumentAssertion(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["required", "enum", "range", "predicate"]
    path: str = Field(min_length=1)
    values: list[Any] | None = None
    minimum: Decimal | None = None
    maximum: Decimal | None = None
    predicate: Literal["non_empty", "https_url", "readonly_sql"] | None = None

    @model_validator(mode="after")
    def assertion_shape(self) -> "ArgumentAssertion":
        if self.kind == "enum" and not self.values:
            raise ValueError("enum assertion requires values")
        if self.kind == "range" and self.minimum is None and self.maximum is None:
            raise ValueError("range assertion requires minimum or maximum")
        if self.kind == "predicate" and self.predicate is None:
            raise ValueError("predicate assertion requires a named deterministic predicate")
        return self


class ToolDecisionLabel(BaseModel):
    model_config = ConfigDict(extra="forbid")
    acceptable_tools: list[str] = Field(min_length=1)
    argument_assertions: list[ArgumentAssertion] = Field(default_factory=list)
    label_source: LabelSource
    confidence: Confidence
    difficulty: Difficulty

    @model_validator(mode="after")
    def unique_tools(self) -> "ToolDecisionLabel":
        if len(self.acceptable_tools) != len(set(self.acceptable_tools)):
            raise ValueError("acceptable_tools must be unique")
        return self


class GovernanceDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    eligible: bool
    reasons: list[str]
    context: dict[str, Any] | None = None
    decision: dict[str, Any] | None = None


class TrainingPermissionPolicy:
    """Fail-closed tenant/user permission supplied by deployment governance."""

    def __init__(
        self,
        *,
        allowed_tenants: frozenset[str] = frozenset(),
        allowed_users: frozenset[tuple[str, str]] = frozenset(),
    ) -> None:
        self.allowed_tenants = allowed_tenants
        self.allowed_users = allowed_users

    def allows(self, tenant_id: str, user_id: str) -> bool:
        return tenant_id in self.allowed_tenants and (
            tenant_id,
            user_id,
        ) in self.allowed_users


def capability_snapshot_ref(snapshot: dict[str, Any] | CapabilitySnapshot) -> str:
    payload = (
        snapshot.model_dump(mode="json")
        if isinstance(snapshot, CapabilitySnapshot)
        else CapabilitySnapshot.model_validate(snapshot).model_dump(mode="json")
    )
    return sha256_hex(canonical_json(payload))


def bounded_tool_observation(
    tool_name: str,
    result: ToolResult,
    *,
    observation_index: int | None = None,
    max_summary_chars: int = 512,
    max_inline_chars: int = 2_048,
) -> dict[str, Any]:
    """The exact bounded projection shown to the next model decision and captured."""

    projected: dict[str, Any] = {
        "tool_name": tool_name,
        "status": result.status,
        "summary": result.summary[:max_summary_chars],
        "references": [
            {
                key: value
                for key, value in {
                    "source_uri": ref.source_uri,
                    "doc_id": ref.doc_id,
                    "chunk_id": ref.chunk_id,
                    "workspace_path": ref.workspace_path,
                    "content_hash": ref.content_hash,
                }.items()
                if value is not None
            }
            for ref in result.references[:8]
        ],
    }
    if observation_index is not None:
        projected["observation_index"] = observation_index
    if result.error is not None:
        projected["error"] = {
            "type": result.error.type,
            "code": result.error.code,
            "retryable": result.error.retryable,
        }
    if result.data is not None:
        rendered = canonical_json(result.data)
        projected["data"] = result.data if len(rendered) <= max_inline_chars else {
            "content_hash": sha256_hex(rendered),
            "omitted": "inline_result_exceeded_decision_context_limit",
        }
    return projected


class DecisionTraceService:
    """Best-effort side writer; callers decide how to isolate capture failures."""

    def __init__(self, session_factory: SessionFactory) -> None:
        self.session_factory = session_factory

    @staticmethod
    def decision_id(
        tenant_id: str, task_id: str, node_run_id: str, decision_index: int
    ) -> str:
        digest = sha256_hex(
            canonical_json([tenant_id, task_id, node_run_id, decision_index])
        )
        return f"decision_{digest[:48]}"

    def capture(
        self,
        *,
        tenant_id: str,
        user_id: str,
        task_id: str,
        thread_id: str,
        node_run_id: str,
        decision_index: int,
        agent_name: str,
        model_binding: dict[str, Any],
        context: ToolDecisionContext,
        decision: ToolDecisionValue,
    ) -> str:
        if agent_name not in EXECUTION_AGENTS:
            raise ValueError("Only execution-agent Tool Decisions may be captured")
        decision_id = self.decision_id(
            tenant_id, task_id, node_run_id, decision_index
        )
        payload_context = context.model_dump(mode="json")
        payload_decision = decision.model_dump(mode="json", exclude_none=True)
        try:
            with self.session_factory() as session, session.begin():
                existing = session.get(DecisionTraceRow, decision_id)
                if existing is not None:
                    self._mark_replay_mismatch(
                        existing,
                        model_binding=model_binding,
                        context=payload_context,
                        decision=payload_decision,
                    )
                    return existing.decision_id
                session.add(
                    DecisionTraceRow(
                        decision_id=decision_id,
                        tenant_id=tenant_id,
                        user_id=user_id,
                        task_id=task_id,
                        thread_id=thread_id,
                        node_run_id=node_run_id,
                        decision_index=decision_index,
                        agent_name=agent_name,
                        model_binding=model_binding,
                        context=payload_context,
                        decision=payload_decision,
                    )
                )
            return decision_id
        except IntegrityError:
            with self.session_factory() as session:
                existing = session.get(DecisionTraceRow, decision_id)
                if existing is None:
                    raise
                return existing.decision_id

    @staticmethod
    def _mark_replay_mismatch(
        row: DecisionTraceRow,
        *,
        model_binding: dict[str, Any],
        context: dict[str, Any],
        decision: dict[str, Any],
    ) -> None:
        if (
            row.model_binding == model_binding
            and row.context == context
            and row.decision == decision
        ):
            return
        outcome = dict(row.outcome or {})
        outcome["data_contamination"] = True
        outcome["contamination_reason"] = "semantic_position_replayed_with_different_input"
        row.outcome = outcome
        row.updated_at = utcnow()

    def record_execution(self, decision_id: str, execution: ToolDecisionExecution) -> None:
        with self.session_factory() as session, session.begin():
            row = session.get(DecisionTraceRow, decision_id)
            if row is not None:
                row.execution = execution.model_dump(mode="json", exclude_none=True)
                row.updated_at = utcnow()

    def record_outcome(self, decision_id: str, **values: bool) -> None:
        allowed = {"result_accepted", "evidence_created"}
        if set(values) - allowed:
            raise ValueError("Unsupported Tool Decision outcome field")
        with self.session_factory() as session, session.begin():
            row = session.get(DecisionTraceRow, decision_id)
            if row is not None:
                outcome = dict(row.outcome or {})
                outcome.update(values)
                row.outcome = ToolDecisionOutcome.model_validate(outcome).model_dump(
                    mode="json", exclude_none=True
                )
                row.updated_at = utcnow()

    def list_candidates(
        self,
        *,
        tenant_id: str,
        user_id: str | None = None,
        created_from: datetime | None = None,
        created_to: datetime | None = None,
    ) -> list[DecisionTraceRow]:
        statement = select(DecisionTraceRow).where(
            DecisionTraceRow.tenant_id == tenant_id
        )
        if user_id is not None:
            statement = statement.where(DecisionTraceRow.user_id == user_id)
        if created_from is not None:
            statement = statement.where(DecisionTraceRow.created_at >= created_from)
        if created_to is not None:
            statement = statement.where(DecisionTraceRow.created_at < created_to)
        statement = statement.order_by(
            DecisionTraceRow.created_at,
            DecisionTraceRow.decision_id,
        )
        with self.session_factory() as session:
            return list(session.scalars(statement))


class SensitiveDataRedactor:
    VERSION = "redaction-v1"
    _EMAIL = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.I)
    _PHONE = re.compile(r"(?<!\d)(?:\+?\d[\d -]{7,}\d)(?!\d)")
    _BEARER = re.compile(r"(?i)bearer\s+[A-Za-z0-9._~+/=-]{8,}")
    _SECRET = re.compile(
        r"(?i)(api[_-]?key|secret|password|credential|token)\s*[:=]\s*[^\s,;]+"
    )
    _SIGNED_QUERY = re.compile(
        r"(?i)([?&](?:x-amz-signature|signature|sig|token|access_key)=[^&#\s]+)"
    )
    _STORAGE = re.compile(r"(?i)\b(?:s3|minio|file)://[^\s]+")

    def redact(self, value: Any, *, direct_ids: frozenset[str]) -> Any:
        if isinstance(value, dict):
            redacted: dict[str, Any] = {}
            for key, item in value.items():
                normalized_key = str(key).lower()
                if normalized_key in {
                    "credential",
                    "credentials",
                    "secret",
                    "password",
                    "api_key",
                }:
                    continue
                if normalized_key in {
                    "schema_hash",
                    "toolset_snapshot_ref",
                    "content_hash",
                }:
                    redacted[str(key)] = item
                else:
                    redacted[str(key)] = self.redact(item, direct_ids=direct_ids)
            return redacted
        if isinstance(value, list):
            return [self.redact(item, direct_ids=direct_ids) for item in value]
        if not isinstance(value, str):
            return value
        rendered = value
        for direct_id in sorted(direct_ids, key=len, reverse=True):
            if direct_id:
                rendered = rendered.replace(direct_id, "[BUSINESS_ID]")
        rendered = self._EMAIL.sub("[PII_EMAIL]", rendered)
        rendered = self._PHONE.sub("[PII_PHONE]", rendered)
        rendered = self._BEARER.sub("Bearer [CREDENTIAL]", rendered)
        rendered = self._SECRET.sub(lambda match: f"{match.group(1)}=[CREDENTIAL]", rendered)
        rendered = self._SIGNED_QUERY.sub("", rendered)
        return self._STORAGE.sub("[STORAGE_REFERENCE]", rendered)


@dataclass(frozen=True)
class GovernedToolDecision:
    decision_id: str
    tenant_id: str
    thread_id: str
    agent_name: str
    created_at: datetime
    context: ToolDecisionContext
    decision: ToolDecisionValue
    tool_schemas: dict[str, dict[str, Any]]
    execution: ToolDecisionExecution | None
    outcome: dict[str, Any]


class ToolDecisionGovernance:
    def __init__(
        self,
        session_factory: SessionFactory,
        registry: ToolRegistry,
        permission_policy: TrainingPermissionPolicy,
        *,
        redactor: SensitiveDataRedactor | None = None,
    ) -> None:
        self.session_factory = session_factory
        self.registry = registry
        self.permission_policy = permission_policy
        self.redactor = redactor or SensitiveDataRedactor()

    def evaluate(
        self, row: DecisionTraceRow
    ) -> tuple[GovernanceDecision, GovernedToolDecision | None]:
        reasons: list[str] = []
        if not self.permission_policy.allows(row.tenant_id, row.user_id):
            reasons.append("training_permission_denied")
        if row.agent_name not in EXECUTION_AGENTS:
            reasons.append("agent_out_of_scope")
        with self.session_factory() as session:
            task = session.scalar(
                select(ResearchTaskRow).where(
                    ResearchTaskRow.tenant_id == row.tenant_id,
                    ResearchTaskRow.task_id == row.task_id,
                )
            )
        if task is None:
            reasons.append("task_or_capability_snapshot_unavailable")
            return GovernanceDecision(eligible=False, reasons=reasons), None
        if task.user_id != row.user_id or task.thread_id != row.thread_id:
            reasons.append("scope_mismatch")

        try:
            snapshot = CapabilitySnapshot.model_validate(task.capability_snapshot)
            context = ToolDecisionContext.model_validate(row.context)
            decision = ToolDecisionValue.model_validate(row.decision)
        except ValueError:
            reasons.append("trace_contract_invalid")
            return GovernanceDecision(eligible=False, reasons=reasons), None

        if capability_snapshot_ref(snapshot) != context.toolset_snapshot_ref:
            reasons.append("capability_snapshot_mismatch")

        task_bindings = dict(task.model_binding_snapshot.get("bindings") or {})
        binding_key = {"deep_scout": "tool", "data_analyst": "data", "code_wizard": "code"}[
            row.agent_name
        ]
        if task_bindings.get(binding_key) != row.model_binding:
            reasons.append("model_binding_unrecoverable")

        tool_schemas: dict[str, dict[str, Any]] = {}
        available_names: set[str] = set()
        for available in context.available_tools:
            frozen = snapshot.tool(available.name)
            if frozen is None or frozen.schema_hash != available.schema_hash:
                reasons.append("tool_schema_mismatch")
                continue
            try:
                definition = self.registry.resolve(frozen)
            except KeyError:
                reasons.append("tool_schema_unrecoverable")
                continue
            tool_schemas[available.name] = (
                definition.schema_json or definition.args_schema.model_json_schema()
            )
            available_names.add(available.name)

        if decision.selected_tool != NO_TOOL:
            if decision.selected_tool not in available_names:
                reasons.append("selected_tool_not_visible")
            else:
                frozen = snapshot.tool(decision.selected_tool)
                assert frozen is not None
                try:
                    definition = self.registry.resolve(frozen)
                    definition.args_schema.model_validate(decision.arguments or {})
                except (KeyError, ValueError):
                    reasons.append("arguments_schema_invalid")

        execution = (
            ToolDecisionExecution.model_validate(row.execution)
            if row.execution is not None
            else None
        )
        if execution is not None and execution.normalized_error in {
            "policy_denied",
            "tool_denied",
            "capability_not_frozen",
        }:
            reasons.append("permission_contamination")
        if (row.outcome or {}).get("data_contamination"):
            reasons.append("data_contamination")

        direct_ids = frozenset({row.tenant_id, row.user_id, row.task_id, row.thread_id})
        redacted_context = self.redactor.redact(
            context.model_dump(mode="json"), direct_ids=direct_ids
        )
        redacted_decision = self.redactor.redact(
            decision.model_dump(mode="json", exclude_none=True), direct_ids=direct_ids
        )
        try:
            governed_context = ToolDecisionContext.model_validate(redacted_context)
            governed_decision = ToolDecisionValue.model_validate(redacted_decision)
        except ValueError:
            reasons.append("redaction_broke_contract")
            governed_context = context
            governed_decision = decision

        if reasons:
            return (
                GovernanceDecision(
                    eligible=False,
                    reasons=sorted(set(reasons)),
                    context=redacted_context,
                    decision=redacted_decision,
                ),
                None,
            )
        governed = GovernedToolDecision(
            decision_id=row.decision_id,
            tenant_id=row.tenant_id,
            thread_id=row.thread_id,
            agent_name=row.agent_name,
            created_at=row.created_at,
            context=governed_context,
            decision=governed_decision,
            tool_schemas=tool_schemas,
            execution=execution,
            outcome=dict(row.outcome or {}),
        )
        return (
            GovernanceDecision(
                eligible=True,
                reasons=[],
                context=governed_context.model_dump(mode="json"),
                decision=governed_decision.model_dump(mode="json", exclude_none=True),
            ),
            governed,
        )


class ToolLabelBuilder:
    def build(
        self,
        governed: GovernedToolDecision,
        *,
        acceptable_tools: list[str],
        argument_assertions: list[ArgumentAssertion] | None = None,
        label_source: LabelSource,
        confidence: Confidence,
        difficulty: Difficulty,
        reviewed: bool = False,
    ) -> ToolDecisionLabel:
        visible = set(governed.tool_schemas) | {NO_TOOL}
        if not set(acceptable_tools).issubset(visible):
            raise ValueError("Label contains a Tool that was not visible at decision time")
        if label_source in {"human_gold", "validated_outcome"} and not reviewed:
            raise ValueError(f"{label_source} requires an explicit review decision")
        return ToolDecisionLabel(
            acceptable_tools=acceptable_tools,
            argument_assertions=argument_assertions or [],
            label_source=label_source,
            confidence=confidence,
            difficulty=difficulty,
        )


class ToolDecisionReviewService:
    def __init__(
        self,
        session_factory: SessionFactory,
        audit_service: AuditService | None = None,
    ) -> None:
        self.session_factory = session_factory
        self.audit_service = audit_service

    def submit(
        self,
        *,
        decision_id: str,
        reviewer_ref: str,
        disposition: Literal["accepted", "excluded"],
        label: ToolDecisionLabel | None,
    ) -> ToolDecisionReviewRow:
        if disposition == "accepted" and label is None:
            raise ValueError("Accepted review requires a Tool Decision label")
        if disposition == "excluded" and label is not None:
            raise ValueError("Excluded review cannot carry a training label")
        with self.session_factory() as session, session.begin():
            trace = session.get(DecisionTraceRow, decision_id)
            if trace is None:
                raise KeyError(decision_id)
            revision = int(
                session.scalar(
                    select(func.max(ToolDecisionReviewRow.revision)).where(
                        ToolDecisionReviewRow.decision_id == decision_id
                    )
                )
                or 0
            ) + 1
            review = ToolDecisionReviewRow(
                review_id=f"review_{sha256_hex(f'{decision_id}:{revision}')[:48]}",
                decision_id=decision_id,
                revision=revision,
                disposition=disposition,
                label=label.model_dump(mode="json") if label else None,
                reviewer_ref=reviewer_ref,
            )
            session.add(review)
        if self.audit_service is not None:
            self.audit_service.append(
                tenant_id=trace.tenant_id,
                actor_ref=reviewer_ref,
                action="tool_decision.review",
                resource_ref=f"decision_trace:{decision_id}",
                result="succeeded",
                reason_code=disposition,
            )
        return review

    def latest(self, decision_id: str) -> ToolDecisionReviewRow | None:
        with self.session_factory() as session:
            return session.scalar(
                select(ToolDecisionReviewRow)
                .where(ToolDecisionReviewRow.decision_id == decision_id)
                .order_by(ToolDecisionReviewRow.revision.desc())
                .limit(1)
            )


def argument_assertions_pass(
    arguments: dict[str, Any], assertions: list[ArgumentAssertion]
) -> bool:
    for assertion in assertions:
        found, value = _value_at_path(arguments, assertion.path)
        if assertion.kind == "required":
            if not found:
                return False
        elif assertion.kind == "enum":
            if not found or value not in (assertion.values or []):
                return False
        elif assertion.kind == "range":
            try:
                numeric = Decimal(str(value))
            except Exception:
                return False
            if assertion.minimum is not None and numeric < assertion.minimum:
                return False
            if assertion.maximum is not None and numeric > assertion.maximum:
                return False
        elif not _predicate_passes(assertion.predicate, value if found else None):
            return False
    return True


def _value_at_path(arguments: dict[str, Any], path: str) -> tuple[bool, Any]:
    current: Any = arguments
    for part in path.split("."):
        if not isinstance(current, dict) or part not in current:
            return False, None
        current = current[part]
    return True, current


def _predicate_passes(name: str | None, value: Any) -> bool:
    if name == "non_empty":
        return isinstance(value, (str, list, dict)) and bool(value)
    if name == "https_url":
        return isinstance(value, str) and value.startswith("https://")
    if name == "readonly_sql":
        if not isinstance(value, str):
            return False
        normalized = value.strip().lower()
        return normalized.startswith(("select ", "with ")) and not re.search(
            r"\b(insert|update|delete|drop|alter|truncate|grant|revoke)\b",
            normalized,
        )
    return False


@dataclass(frozen=True)
class BuiltToolSample:
    sample_id: str
    thread_id: str
    created_at: datetime
    payload: dict[str, Any]


class ToolSampleBuilder:
    FORMAT_VERSION = "native-tool-call-v1"

    def build(
        self,
        governed: GovernedToolDecision,
        label: ToolDecisionLabel,
    ) -> BuiltToolSample:
        if label.label_source == "curated_eval":
            raise ValueError("Formal curated Evaluation cases cannot enter Flywheel snapshots")
        selected = governed.decision.selected_tool
        if selected not in label.acceptable_tools:
            raise ValueError("Observed production decision is not an acceptable training target")
        arguments = governed.decision.arguments or {}
        if selected != NO_TOOL and not argument_assertions_pass(
            arguments, label.argument_assertions
        ):
            raise ValueError("Observed arguments do not satisfy the reviewed label")
        tools = [
            {
                "type": "function",
                "function": {
                    "name": item.name,
                    "parameters": governed.tool_schemas[item.name],
                },
            }
            for item in governed.context.available_tools
        ]
        messages: list[dict[str, Any]] = [
            {
                "role": "system",
                "content": f"You are the {governed.agent_name} execution agent.",
            },
            {"role": "user", "content": governed.context.current_intent},
        ]
        messages.extend(
            {
                "role": "tool",
                "name": str(item.get("tool_name") or "tool"),
                "content": canonical_json(item).decode("utf-8"),
            }
            for item in governed.context.prior_tool_results
        )
        output: dict[str, Any]
        if selected == NO_TOOL:
            output = {"role": "assistant", "content": NO_TOOL, "tool_calls": []}
        else:
            output = {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call_0",
                        "type": "function",
                        "function": {
                            "name": selected,
                            "arguments": canonical_json(arguments).decode("utf-8"),
                        },
                    }
                ],
            }
        sample_id = f"sample_{sha256_hex(governed.decision_id)[:48]}"
        payload = {
            "sample_id": sample_id,
            "trace_type": "tool",
            "agent": governed.agent_name,
            "difficulty": label.difficulty,
            "input": {"messages": messages, "tools": tools},
            "output": output,
            "label": label.model_dump(mode="json"),
            "diagnostics": {
                "execution_status": (
                    governed.execution.status if governed.execution is not None else None
                ),
                "normalized_error": (
                    governed.execution.normalized_error
                    if governed.execution is not None
                    else None
                ),
            },
        }
        return BuiltToolSample(
            sample_id=sample_id,
            thread_id=governed.thread_id,
            created_at=governed.created_at,
            payload=payload,
        )


def canonical_hash(value: Any) -> str:
    return hashlib.sha256(canonical_json(value)).hexdigest()
