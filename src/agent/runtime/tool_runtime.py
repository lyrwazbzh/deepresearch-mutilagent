from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from contextlib import nullcontext
from datetime import datetime, timezone
from typing import Any, Callable, Literal, Protocol

import httpx
from opentelemetry.trace import SpanKind
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from src.agent.schema.capability import CapabilitySnapshot, FrozenTool
from src.agent.identity import canonical_json, operation_key, sha256_hex
from src.agent.schema.runtime import ExecutionContext
from src.agent.backends.persistence.database import SessionFactory
from src.agent.backends.audit import AuditService, current_trace_id
from src.agent.backends.observability import Telemetry, TraceScope, bind_span_event_target
from src.agent.runtime.budget import BudgetDelta, BudgetExceededError, TaskBudgetManager

ToolErrorType = Literal[
    "invalid_argument",
    "policy_denied",
    "not_found",
    "conflict",
    "timeout",
    "rate_limited",
    "provider_auth_error",
    "provider_unavailable",
    "limit_exceeded",
    "tool_error",
    "cancelled",
]


class ToolError(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: ToolErrorType
    code: str | None = None
    message: str
    retryable: bool = False


class ToolReference(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source_uri: str | None = None
    canonical_url: str | None = None
    published_at: datetime | None = None
    retrieved_at: datetime | None = None
    doc_id: str | None = None
    chunk_id: str | None = None
    workspace_path: str | None = None
    content_hash: str | None = None
    mime_type: str | None = None
    size: int | None = None


class ToolTrace(BaseModel):
    model_config = ConfigDict(extra="forbid")
    tool_name: str
    operation_key: str
    node_run_id: str
    evidence_eligible: bool = False
    replayed: bool = False
    metadata: dict[str, Any] = Field(default_factory=dict)


class ToolResult(BaseModel):
    """Formal ToolResult; legacy Phase-1 constructor words are only translated in-process."""

    model_config = ConfigDict(extra="forbid")
    status: Literal["success", "error"]
    summary: str = ""
    data: dict[str, Any] | None = None
    references: list[ToolReference] = Field(default_factory=list)
    truncated: bool | None = None
    next_cursor: str | None = None
    error: ToolError | None = None
    trace: ToolTrace | None = None

    def __init__(self, **data: Any) -> None:
        if "ok" in data:
            ok = bool(data.pop("ok"))
            content = data.pop("content", None)
            error_code = data.pop("error_code", None)
            data.setdefault("status", "success" if ok else "error")
            if ok:
                data.setdefault("data", content or {})
            else:
                data.setdefault(
                    "error",
                    ToolError(
                        type="policy_denied" if error_code == "tool_denied" else "tool_error",
                        code=error_code,
                        message=error_code or "tool failed",
                    ),
                )
        super().__init__(**data)

    @property
    def ok(self) -> bool:
        return self.status == "success"

    @property
    def content(self) -> dict[str, Any]:
        return self.data or {}

    @property
    def error_code(self) -> str | None:
        return self.error.code if self.error else None


@dataclass(frozen=True)
class ToolExecutionContext:
    tenant_id: str
    user_id: str
    task_id: str
    thread_id: str
    node_run_id: str
    cancellation_token: Any
    deadline: datetime
    inline_budget: int
    operation_key: str
    resource_scope: dict[str, Any] = field(default_factory=dict)


class ToolAdapter(Protocol):
    def execute(
        self, context: ToolExecutionContext, normalized_arguments: dict[str, Any]
    ) -> ToolResult: ...


class Tool(Protocol):
    name: str

    def invoke(self, arguments: dict[str, Any], context: ExecutionContext) -> ToolResult: ...


class AnyArguments(BaseModel):
    model_config = ConfigDict(extra="allow")


@dataclass(frozen=True)
class ToolDefinition:
    name: str
    args_schema: type[BaseModel]
    adapter: ToolAdapter
    evidence_eligible: bool = False
    search_call: bool = False
    effect_target: Callable[[dict[str, Any]], str] | None = None
    scope_validator: Callable[[ToolExecutionContext, dict[str, Any]], None] | None = None
    data_with_workspace_is_metadata: bool = False
    provider_ref: str = "local:builtin:v1"
    schema_json: dict[str, Any] | None = None

    @property
    def schema_hash(self) -> str:
        schema = self.schema_json or self.args_schema.model_json_schema()
        return sha256_hex(canonical_json(schema))

    def frozen(self) -> FrozenTool:
        return FrozenTool(
            name=self.name,
            schema_hash=self.schema_hash,
            provider_ref=self.provider_ref,
        )


class LegacyToolAdapter:
    def __init__(self, tool: Tool) -> None:
        self.tool = tool

    def execute(
        self, context: ToolExecutionContext, normalized_arguments: dict[str, Any]
    ) -> ToolResult:
        execution = ExecutionContext(
            tenant_id=context.tenant_id,
            user_id=context.user_id,
            task_id=context.task_id,
            thread_id=context.thread_id,
            lease_token="",
            cancellation_token=context.cancellation_token,
            deadline=context.deadline,
        )
        return self.tool.invoke(normalized_arguments, execution)


class ToolRegistry:
    def __init__(self, tools: list[Tool | ToolDefinition] | None = None) -> None:
        self._tools: dict[str, ToolDefinition] = {}
        self._versions: dict[tuple[str, str, str], ToolDefinition] = {}
        for tool in tools or []:
            if isinstance(tool, ToolDefinition):
                self.register(tool)
            else:
                self.register(
                    ToolDefinition(
                        name=tool.name,
                        args_schema=AnyArguments,
                        adapter=LegacyToolAdapter(tool),
                    )
                )

    def register(self, definition: ToolDefinition) -> None:
        if definition.name in self._tools:
            raise ValueError(f"Tool already registered: {definition.name}")
        self._tools[definition.name] = definition
        self._versions[
            (definition.name, definition.schema_hash, definition.provider_ref)
        ] = definition

    def replace(self, definition: ToolDefinition) -> None:
        if definition.name not in self._tools:
            raise KeyError(f"Tool is not registered: {definition.name}")
        self._tools[definition.name] = definition
        self._versions[
            (definition.name, definition.schema_hash, definition.provider_ref)
        ] = definition

    def get(self, name: str) -> ToolDefinition | None:
        return self._tools.get(name)

    def names(self) -> list[str]:
        return sorted(self._tools)

    def snapshot(self) -> list[FrozenTool]:
        return [self._tools[name].frozen() for name in sorted(self._tools)]

    def resolve(self, frozen: FrozenTool) -> ToolDefinition:
        try:
            return self._versions[(frozen.name, frozen.schema_hash, frozen.provider_ref)]
        except KeyError as exc:
            raise KeyError(
                "Frozen Tool contract is unavailable: "
                f"{frozen.name}@{frozen.schema_hash}:{frozen.provider_ref}"
            ) from exc


@dataclass(frozen=True)
class ToolPolicy:
    allowed_tools: frozenset[str]

    def allows(self, tool_name: str) -> bool:
        return tool_name in self.allowed_tools


class ToolPolicyRegistry:
    """Immutable policy revisions used by active Tasks after configuration changes."""

    def __init__(self, policies: dict[str, ToolPolicy] | None = None) -> None:
        self._versions: dict[str, dict[str, ToolPolicy]] = {}
        if policies is not None:
            self.register(policies)

    @staticmethod
    def revision(policies: dict[str, ToolPolicy]) -> str:
        payload = {
            agent: sorted(policy.allowed_tools)
            for agent, policy in sorted(policies.items())
        }
        return hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()

    def register(self, policies: dict[str, ToolPolicy]) -> str:
        revision = self.revision(policies)
        self._versions[revision] = dict(policies)
        return revision

    def resolve(self, revision: str, agent_name: str) -> ToolPolicy:
        try:
            policies = self._versions[revision]
        except KeyError as exc:
            raise KeyError(f"Frozen Tool Policy revision is unavailable: {revision}") from exc
        return policies.get(agent_name, ToolPolicy(frozenset()))


class ToolRuntime:
    def __init__(
        self,
        registry: ToolRegistry,
        *,
        session_factory: SessionFactory | None = None,
        inline_budget: int = 32_000,
        telemetry: Telemetry | None = None,
        audit_service: AuditService | None = None,
    ) -> None:
        self.registry = registry
        self.session_factory = session_factory
        self.inline_budget = inline_budget
        self.telemetry = telemetry
        self.audit_service = audit_service
        self.budget_manager = (
            TaskBudgetManager(session_factory) if session_factory is not None else None
        )

    def execute(
        self,
        *,
        agent_name: str,
        tool_name: str,
        arguments: dict[str, Any],
        execution: ExecutionContext,
        node_run_id: str,
        policy: ToolPolicy,
        resource_scope: dict[str, Any] | None = None,
        logical_call_slot: str | None = None,
    ) -> ToolResult:
        span_context = (
            self.telemetry.span(
                "tool.execute",
                scope=TraceScope(
                    tenant_id=execution.tenant_id,
                    user_id=execution.user_id,
                    task_id=execution.task_id,
                ),
                attributes={
                    "tool.name": tool_name,
                    "research.agent": agent_name,
                    "research.node_run_id": node_run_id,
                },
            )
            if self.telemetry is not None
            else nullcontext()
        )
        with span_context as span:
            with bind_span_event_target(span):
                result = self._execute(
                    agent_name=agent_name,
                    tool_name=tool_name,
                    arguments=arguments,
                    execution=execution,
                    node_run_id=node_run_id,
                    policy=policy,
                    resource_scope=resource_scope,
                    logical_call_slot=logical_call_slot,
                )
            if result.error is not None and span is not None:
                span.set_attribute("error.type", result.error.code or result.error.type)
                category = (
                    "provider"
                    if result.error.type
                    in {"rate_limited", "provider_auth_error", "provider_unavailable"}
                    else "infrastructure"
                )
                span.set_attribute("error.category", category)
            if result.error is not None and result.error.type == "policy_denied":
                self._audit_denial(execution, tool_name, result.error.code or "policy_denied")
            return result

    def _execute(
        self,
        *,
        agent_name: str,
        tool_name: str,
        arguments: dict[str, Any],
        execution: ExecutionContext,
        node_run_id: str,
        policy: ToolPolicy,
        resource_scope: dict[str, Any] | None = None,
        logical_call_slot: str | None = None,
    ) -> ToolResult:
        if execution.cancellation_token.is_cancelled():
            return self._error("cancelled", "cancelled", "Tool call was cancelled.")
        if datetime.now(timezone.utc) >= execution.deadline:
            return self._error("timeout", "deadline_exceeded", "Tool deadline expired.")
        if not policy.allows(tool_name):
            return self._error(
                "policy_denied", "tool_denied", f"{agent_name} cannot call {tool_name}."
            )
        definition: ToolDefinition | None
        if execution.capability_snapshot is not None:
            try:
                snapshot = CapabilitySnapshot.model_validate(execution.capability_snapshot)
                frozen_tool = snapshot.tool(tool_name)
                if frozen_tool is None:
                    return self._error(
                        "policy_denied",
                        "capability_not_frozen",
                        f"Tool {tool_name} is not frozen for this Task.",
                    )
                definition = self.registry.resolve(frozen_tool)
            except (KeyError, ValueError) as exc:
                return self._error(
                    "not_found",
                    "frozen_capability_unavailable",
                    str(exc),
                )
        else:
            definition = self.registry.get(tool_name)
        if definition is None:
            return self._error(
                "not_found", "tool_unavailable", f"Tool {tool_name} is unavailable."
            )
        try:
            normalized = definition.args_schema.model_validate(arguments).model_dump(
                mode="python", exclude_none=True
            )
        except ValidationError as exc:
            return self._error("invalid_argument", "schema_validation", str(exc))

        target = logical_call_slot or (
            definition.effect_target(normalized)
            if definition.effect_target
            else hashlib.sha256(
                repr(sorted(normalized.items())).encode("utf-8")
            ).hexdigest()[:24]
        )
        effect_name = f"{tool_name}:{target}"
        if len(effect_name) > 128:
            effect_name = f"{tool_name}:{hashlib.sha256(target.encode()).hexdigest()}"
        op_key = operation_key(
            execution.tenant_id, execution.task_id, node_run_id, effect_name
        )
        context = ToolExecutionContext(
            tenant_id=execution.tenant_id,
            user_id=execution.user_id,
            task_id=execution.task_id,
            thread_id=execution.thread_id,
            node_run_id=node_run_id,
            cancellation_token=execution.cancellation_token,
            deadline=execution.deadline,
            inline_budget=self.inline_budget,
            operation_key=op_key,
            resource_scope=resource_scope or {},
        )
        try:
            if definition.scope_validator:
                definition.scope_validator(context, normalized)
        except PermissionError as exc:
            return self._error("policy_denied", "resource_scope", str(exc))

        try:
            reservation = (
                self.budget_manager.reserve_effect(
                    tenant_id=execution.tenant_id,
                    task_id=execution.task_id,
                    node_run_id=node_run_id,
                    effect_name=effect_name,
                    operation_key=op_key,
                    upper_bound=BudgetDelta(
                        search_calls=1 if definition.search_call else 0
                    ),
                )
                if self.budget_manager is not None
                else None
            )
        except BudgetExceededError as exc:
            return self._error(
                "limit_exceeded",
                "budget_exhausted",
                str(exc),
            )
        if reservation is not None and reservation.replay_payload is not None:
            cached = ToolResult.model_validate(reservation.replay_payload)
            trace = (
                cached.trace.model_copy(update={"replayed": True})
                if cached.trace
                else ToolTrace(
                    tool_name=tool_name,
                    operation_key=op_key,
                    node_run_id=node_run_id,
                    evidence_eligible=definition.evidence_eligible,
                    replayed=True,
                )
            )
            return cached.model_copy(update={"trace": trace})

        adapter_invoked = False
        try:
            adapter_invoked = True
            client_context = (
                self.telemetry.span(
                    "tool.client",
                    kind=SpanKind.CLIENT,
                    attributes={
                        "client.operation": tool_name,
                        "server.address": definition.provider_ref,
                        "tool.name": tool_name,
                    },
                )
                if self.telemetry is not None
                else nullcontext()
            )
            with client_context:
                result = self._normalize(
                    definition.adapter.execute(context, normalized),
                    allow_workspace_metadata=definition.data_with_workspace_is_metadata,
                )
        except InterruptedError:
            result = self._error(
                "cancelled", "cancelled", "Tool execution was cancelled."
            )
        except TimeoutError:
            result = self._error(
                "timeout", "timeout", "Tool execution timed out.", retryable=True
            )
        except FileNotFoundError as exc:
            result = self._error("not_found", "not_found", str(exc))
        except PermissionError as exc:
            result = self._error(
                "policy_denied",
                str(getattr(exc, "reason_code", "permission_denied")),
                "Security or resource policy denied the Tool call.",
            )
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            if status in {401, 403}:
                result = self._error(
                    "provider_auth_error", "provider_auth", "Provider rejected credentials."
                )
            elif status == 429:
                result = self._error(
                    "rate_limited",
                    "provider_rate_limit",
                    "Provider rate limited the call.",
                    retryable=True,
                )
            elif status >= 500:
                result = self._error(
                    "provider_unavailable",
                    "http_5xx",
                    "Provider is unavailable.",
                    retryable=True,
                )
            else:
                result = self._error(
                    "tool_error", "provider_http", "Provider request failed."
                )
        except (httpx.TransportError, ConnectionError):
            result = self._error(
                "provider_unavailable",
                "transport",
                "Provider is unavailable.",
                retryable=True,
            )
        except Exception as exc:
            result = self._error("tool_error", "adapter_error", str(exc))

        result = result.model_copy(
            update={
                "trace": ToolTrace(
                    tool_name=tool_name,
                    operation_key=op_key,
                    node_run_id=node_run_id,
                    evidence_eligible=definition.evidence_eligible,
                    metadata={
                        "agent": agent_name,
                        "schema_hash": definition.schema_hash,
                        "provider_ref": definition.provider_ref,
                    },
                )
            }
        )
        if self.budget_manager is not None:
            self.budget_manager.settle_effect(
                task_id=execution.task_id,
                operation_key=op_key,
                actual_usage=BudgetDelta(
                    search_calls=(
                        1 if definition.search_call and adapter_invoked else 0
                    )
                ),
                result_kind="tool",
                result_payload=result.model_dump(mode="json"),
            )
        return result

    def _audit_denial(
        self, execution: ExecutionContext, tool_name: str, reason_code: str
    ) -> None:
        if self.audit_service is None:
            return
        action = (
            "tool.ssrf.denied" if reason_code.startswith("ssrf_") else "tool.policy.denied"
        )
        try:
            self.audit_service.append(
                tenant_id=execution.tenant_id,
                actor_ref=self.audit_service.user_actor(execution.user_id),
                action=action,
                resource_ref=f"tool:{tool_name}",
                result="denied",
                reason_code=reason_code,
                trace_id=current_trace_id(),
            )
        except Exception:
            # Audit availability must not turn an authorization denial into a call.
            return

    @staticmethod
    def _normalize(
        result: ToolResult, *, allow_workspace_metadata: bool = False
    ) -> ToolResult:
        if result.status == "success" and result.error is not None:
            raise ValueError("successful ToolResult cannot contain error")
        if result.status == "error" and result.error is None:
            raise ValueError("failed ToolResult requires error")
        if (
            not allow_workspace_metadata
            and result.data is not None
            and any(ref.workspace_path for ref in result.references)
        ):
            raise ValueError(
                "ToolResult cannot contain complete inline data and Workspace content"
            )
        return result

    @staticmethod
    def _error(
        error_type: ToolErrorType,
        code: str,
        message: str,
        retryable: bool = False,
    ) -> ToolResult:
        return ToolResult(
            status="error",
            error=ToolError(
                type=error_type,
                code=code,
                message=message,
                retryable=retryable,
            ),
        )
