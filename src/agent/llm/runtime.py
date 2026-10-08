from __future__ import annotations

import json
import os
from collections import defaultdict, deque
from collections.abc import Callable
from contextlib import nullcontext
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Protocol, TypeVar

import httpx
from opentelemetry.trace import SpanKind
from langchain_core.callbacks import CallbackManagerForLLMRun
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from pydantic import BaseModel, ConfigDict, PrivateAttr, ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from src.agent.identity import operation_key
from src.agent.schema.model_config import (
    BindingKey,
    FrozenModelBinding,
    ModelBindingSnapshot,
    ModelConfig,
    ResolvedModelRevision,
)
from src.agent.backends.persistence.database import SessionFactory
from src.agent.backends.persistence.models import ModelConfigRow, ModelPricingRow, ResearchTaskRow
from src.agent.backends.persistence.repositories import ModelConfigRepository
from src.agent.backends.observability import Telemetry, TraceScope
from src.agent.runtime.budget import (
    BudgetDelta,
    BudgetExceededError,
    TaskBudgetManager,
    current_runtime_effect_context,
)

T = TypeVar("T", bound=BaseModel)
PHASE1_BINDINGS: tuple[BindingKey, ...] = (
    "routing",
    "reasoning",
    "tool",
    "data",
    "code",
    "writing",
)
PHASE2_BINDINGS: tuple[BindingKey, ...] = (*PHASE1_BINDINGS, "embedding", "rerank")


class ModelConfigurationError(RuntimeError):
    pass


class ModelCallError(RuntimeError):
    def __init__(self, message: str, *, error_code: str = "model_call_failed") -> None:
        super().__init__(message)
        self.error_code = error_code


def _provider_error_code(exc: Exception) -> str:
    if isinstance(exc, httpx.TimeoutException):
        return "timeout"
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        if status in {401, 403}:
            return "authentication_failed"
        if status == 404:
            return "model_or_endpoint_not_found"
        if status == 429:
            return "rate_limited"
        return f"provider_http_{status}"
    return "provider_error"


class ChatModel(Protocol):
    @property
    def binding_identity(self) -> dict[str, Any]: ...

    def invoke_text(self, prompt: str) -> str: ...

    def invoke_structured(self, prompt: str, schema: type[T]) -> T: ...


class ModelResolver:
    """Resolves mutable heads once, at task creation time."""

    def __init__(self) -> None:
        self.resolve_calls = 0

    def resolve_snapshot(
        self,
        session: Session,
        tenant_id: str,
        required_keys: tuple[BindingKey, ...] = PHASE1_BINDINGS,
    ) -> ModelBindingSnapshot:
        self.resolve_calls += 1
        grouped: dict[str, list[tuple[Any, Any]]] = defaultdict(list)
        repository = ModelConfigRepository(session, tenant_id)
        for purpose in ("chat", "embedding", "rerank"):
            for binding, head in repository.bindings(purpose):
                grouped[binding.binding_key].append((binding, head))

        frozen: dict[BindingKey, FrozenModelBinding] = {}
        for key in required_keys:
            candidates = sorted(grouped.get(key, []), key=lambda pair: pair[0].priority)
            if not candidates:
                raise ModelConfigurationError(f"No enabled chat model binding configured for {key}")
            revisions = [
                ResolvedModelRevision(
                    model_config_id=binding.model_config_id,
                    version=head.current_version,
                )
                for binding, head in candidates
            ]
            frozen[key] = FrozenModelBinding(
                binding_key=key,
                purpose="embedding" if key == "embedding" else "rerank" if key == "rerank" else "chat",
                primary=revisions[0],
                fallback=revisions[1:],
            )
        return ModelBindingSnapshot(bindings=frozen)


class ProviderChatResponse(BaseModel):
    content: str
    input_tokens: int = 0
    output_tokens: int = 0


class ModelPricingService:
    def __init__(self, session_factory: SessionFactory) -> None:
        self.session_factory = session_factory

    def upper_bound(
        self,
        *,
        tenant_id: str,
        task_id: str,
        binding: FrozenModelBinding,
        input_tokens: int,
        output_tokens: int,
        attempts_per_revision: int = 2,
        at: datetime | None = None,
    ) -> Decimal:
        at = at or datetime.now(timezone.utc)
        total = Decimal("0")
        with self.session_factory() as session:
            currency = self._task_currency(session, task_id)
            for revision in [binding.primary, *binding.fallback]:
                config = session.get(
                    ModelConfigRow,
                    (revision.model_config_id, revision.version),
                )
                if config is None:
                    continue
                pricing = self._pricing(
                    session,
                    tenant_id,
                    config.provider,
                    config.model,
                    currency,
                    at,
                )
                total += attempts_per_revision * self._amount(
                    pricing, input_tokens, output_tokens
                )
        return total

    def actual_cost(
        self,
        *,
        tenant_id: str,
        task_id: str,
        config: ModelConfig,
        input_tokens: int,
        output_tokens: int,
        at: datetime,
    ) -> Decimal:
        return self.actual_cost_details(
            tenant_id=tenant_id,
            task_id=task_id,
            config=config,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            at=at,
        )[0]

    def actual_cost_details(
        self,
        *,
        tenant_id: str,
        task_id: str | None,
        config: ModelConfig,
        input_tokens: int,
        output_tokens: int,
        at: datetime,
        currency: str | None = None,
    ) -> tuple[Decimal, str]:
        with self.session_factory() as session:
            resolved_currency = currency or (
                self._task_currency(session, task_id) if task_id else "USD"
            )
            pricing = self._pricing(
                session,
                tenant_id,
                config.provider,
                config.model,
                resolved_currency,
                at,
            )
            return (
                self._amount(pricing, input_tokens, output_tokens),
                resolved_currency,
            )

    @staticmethod
    def _task_currency(session: Session, task_id: str) -> str:
        task = session.get(ResearchTaskRow, task_id)
        if task is None:
            raise LookupError(f"ResearchTask {task_id} was not found")
        return str(dict(task.task_budget).get("budget", {}).get("currency") or "USD")

    @staticmethod
    def _pricing(
        session: Session,
        tenant_id: str,
        provider: str,
        model: str,
        currency: str,
        at: datetime,
    ) -> ModelPricingRow | None:
        return session.scalar(
            select(ModelPricingRow)
            .where(
                ModelPricingRow.tenant_id == tenant_id,
                ModelPricingRow.provider == provider,
                ModelPricingRow.model == model,
                ModelPricingRow.currency == currency,
                ModelPricingRow.effective_at <= at,
            )
            .order_by(ModelPricingRow.effective_at.desc())
            .limit(1)
        )

    @staticmethod
    def _amount(
        pricing: ModelPricingRow | None,
        input_tokens: int,
        output_tokens: int,
    ) -> Decimal:
        if pricing is None:
            return Decimal("0")
        return (
            Decimal(input_tokens) * pricing.input_price_per_1m_tokens
            + Decimal(output_tokens) * pricing.output_price_per_1m_tokens
        ) / Decimal(1_000_000)


class ProviderClient(Protocol):
    def chat(self, config: ModelConfig, prompt: str) -> str | ProviderChatResponse: ...

    def embed(self, config: ModelConfig, texts: list[str]) -> list[list[float]]: ...

    def rerank(self, config: ModelConfig, query: str, candidates: list[str]) -> list[float]: ...


class OpenAICompatibleClient:
    def __init__(
        self,
        timeout_seconds: float = 60.0,
        embedding_dimensions: int | None = None,
    ) -> None:
        self.timeout_seconds = timeout_seconds
        self.embedding_dimensions = embedding_dimensions

    def chat(self, config: ModelConfig, prompt: str) -> ProviderChatResponse:
        if not config.base_url:
            raise ModelConfigurationError("base_url is required for OpenAI-compatible chat")
        api_key = os.getenv(config.secret_ref or "") if config.secret_ref else None
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        response = httpx.post(
            f"{config.base_url.rstrip('/')}/chat/completions",
            headers=headers,
            json={
                "model": config.model,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0,
                "max_tokens": 4096,
            },
            timeout=self.timeout_seconds,
        )
        response.raise_for_status()
        payload = response.json()
        usage = payload.get("usage") or {}
        return ProviderChatResponse(
            content=str(payload["choices"][0]["message"]["content"]),
            input_tokens=int(usage.get("prompt_tokens") or 0),
            output_tokens=int(usage.get("completion_tokens") or 0),
        )

    def embed(self, config: ModelConfig, texts: list[str]) -> list[list[float]]:
        if not config.base_url:
            raise ModelConfigurationError("base_url is required for embeddings")
        api_key = os.getenv(config.secret_ref or "") if config.secret_ref else None
        payload: dict[str, Any] = {"model": config.model, "input": texts}
        if self.embedding_dimensions is not None:
            payload["dimensions"] = self.embedding_dimensions
        response = httpx.post(
            f"{config.base_url.rstrip('/')}/embeddings",
            headers={"Authorization": f"Bearer {api_key}"} if api_key else {},
            json=payload,
            timeout=self.timeout_seconds,
        )
        response.raise_for_status()
        return [list(map(float, item["embedding"])) for item in response.json()["data"]]

    def rerank(self, config: ModelConfig, query: str, candidates: list[str]) -> list[float]:
        if not config.base_url:
            raise ModelConfigurationError("base_url is required for rerank")
        api_key = os.getenv(config.secret_ref or "") if config.secret_ref else None
        response = httpx.post(
            f"{config.base_url.rstrip('/')}/rerank",
            headers={"Authorization": f"Bearer {api_key}"} if api_key else {},
            json={"model": config.model, "query": query, "documents": candidates},
            timeout=self.timeout_seconds,
        )
        response.raise_for_status()
        payload = response.json()
        ranked = payload.get("results", payload.get("data", []))
        scores = [0.0] * len(candidates)
        for rank, item in enumerate(ranked):
            index = int(item.get("index", rank))
            scores[index] = float(item.get("relevance_score", item.get("score", 0.0)))
        return scores


class DevelopmentFakeProviderClient:
    """Deterministic local provider enabled only by an explicit fake ModelConfig."""

    def chat(self, config: ModelConfig, prompt: str) -> str:
        del config
        lowered = prompt.lower()
        if "role: routenode" in lowered:
            if "[deep]" in lowered:
                return json.dumps({"mode": "deep"})
            for marker, agent in (
                ("[focused:deep_scout]", "deep_scout"),
                ("[focused:data_analyst]", "data_analyst"),
                ("[focused:code_wizard]", "code_wizard"),
            ):
                if marker in lowered:
                    return json.dumps({"mode": "focused", "target_agent": agent})
            return json.dumps({"mode": "direct"})
        if "role: chiefarchitect / plan" in lowered:
            return json.dumps(
                {
                    "hypotheses": [
                        {
                            "hypothesis_id": "hyp_1",
                            "claim": "The research question has a testable primary explanation.",
                            "evidence_needed": ["Authoritative evidence relevant to the query"],
                            "refuted_if": ["Authoritative evidence contradicts the explanation"],
                            "priority": 1,
                            "queries": [
                                {"query_id": "query_1", "channel": "web", "text": "primary sources"}
                            ],
                        }
                    ],
                    "deepen_history": [],
                }
            )
        if "output contract: agentdecision" in lowered:
            return json.dumps(
                {
                    "action": "final",
                    "answer": "No Phase 1 business tool is registered for this focused request.",
                }
            )
        return "Development fake response (no external model call)."

    def embed(self, config: ModelConfig, texts: list[str]) -> list[list[float]]:
        del config
        return [_deterministic_vector(text) for text in texts]

    def rerank(self, config: ModelConfig, query: str, candidates: list[str]) -> list[float]:
        del config
        query_terms = set(query.lower().split())
        return [float(len(query_terms & set(item.lower().split()))) for item in candidates]


class RuntimeManagedChatModel(BaseChatModel):
    """LangChain ChatModel whose calls remain inside the controlled runtime path."""

    model_config = ConfigDict(arbitrary_types_allowed=True)
    _session_factory: SessionFactory = PrivateAttr()
    _tenant_id: str = PrivateAttr()
    _binding: FrozenModelBinding = PrivateAttr()
    _provider_factory: Callable[[str], ProviderClient] = PrivateAttr()
    _budget_manager: TaskBudgetManager = PrivateAttr()
    _pricing_service: ModelPricingService = PrivateAttr()
    _role: str = PrivateAttr()
    _telemetry: Telemetry | None = PrivateAttr()

    def __init__(
        self,
        session_factory: SessionFactory,
        tenant_id: str,
        binding: FrozenModelBinding,
        provider_factory: Callable[[str], ProviderClient],
        budget_manager: TaskBudgetManager,
        pricing_service: ModelPricingService,
        role: str,
        telemetry: Telemetry | None = None,
    ) -> None:
        super().__init__()
        self._session_factory = session_factory
        self._tenant_id = tenant_id
        self._binding = binding
        self._provider_factory = provider_factory
        self._budget_manager = budget_manager
        self._pricing_service = pricing_service
        self._role = role
        self._telemetry = telemetry

    @property
    def _llm_type(self) -> str:
        return "deepresearch-runtime-managed"

    @property
    def binding_identity(self) -> dict[str, Any]:
        return self._binding.model_dump(mode="json")

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        del stop, run_manager, kwargs
        prompt = "\n".join(f"{message.type}: {message.content}" for message in messages)
        return ChatResult(
            generations=[ChatGeneration(message=AIMessage(content=self.invoke_text(prompt)))]
        )

    def invoke_text(self, prompt: str) -> str:
        runtime_context = current_runtime_effect_context()
        op_key: str | None = None
        task_id: str | None = None
        if runtime_context is not None:
            if runtime_context.execution.cancellation_token.is_cancelled():
                raise ModelCallError(
                    "Model call was cancelled before invocation.",
                    error_code="cancelled",
                )
            if datetime.now(timezone.utc) >= runtime_context.execution.deadline:
                raise ModelCallError(
                    "Model call deadline expired before invocation.",
                    error_code="timeout",
                )
            slot = runtime_context.next_slot(f"model:{self._role}")
            effect_name = f"model:{self._role}:{slot}"
            op_key = operation_key(
                runtime_context.execution.tenant_id,
                runtime_context.execution.task_id,
                runtime_context.node_run_id,
                effect_name,
            )
            task_id = runtime_context.execution.task_id
            attempts = 2 * (1 + len(self._binding.fallback))
            input_bound = max(1, len(prompt.encode("utf-8")))
            cost_bound = self._pricing_service.upper_bound(
                tenant_id=runtime_context.execution.tenant_id,
                task_id=task_id,
                binding=self._binding,
                input_tokens=input_bound,
                output_tokens=4096,
            )
            try:
                reservation = self._budget_manager.reserve_effect(
                    tenant_id=runtime_context.execution.tenant_id,
                    task_id=task_id,
                    node_run_id=runtime_context.node_run_id,
                    effect_name=effect_name,
                    operation_key=op_key,
                    upper_bound=BudgetDelta(
                        model_tokens=(input_bound + 4096) * attempts,
                        cost=cost_bound,
                    ),
                )
            except BudgetExceededError as exc:
                raise ModelCallError(str(exc), error_code="budget_exhausted") from exc
            if reservation.replay_payload is not None:
                if reservation.replay_kind == "model_error":
                    raise ModelCallError(
                        str(reservation.replay_payload.get("message") or "Model call failed"),
                        error_code=str(
                            reservation.replay_payload.get("error_code")
                            or "model_call_failed"
                        ),
                    )
                return str(reservation.replay_payload.get("content") or "")

        failures: list[str] = []
        error_code = "model_call_failed"
        actual_usage = BudgetDelta()
        model_attempt_no = 0
        revisions = [self._binding.primary, *self._binding.fallback]
        for revision_index, revision in enumerate(revisions):
            with self._session_factory() as session:
                repository = ModelConfigRepository(session, self._tenant_id)
                if repository.is_revoked(revision.model_config_id, revision.version):
                    failures.append(f"{revision.model_config_id}@{revision.version}:revoked")
                    continue
                row = repository.revision(revision.model_config_id, revision.version)
                if row is None:
                    failures.append(f"{revision.model_config_id}@{revision.version}:missing")
                    continue
                config = ModelConfig.model_validate(
                    {
                        "model_config_id": row.model_config_id,
                        "version": row.version,
                        "tenant_id": row.tenant_id,
                        "user_id": row.user_id,
                        "provider": row.provider,
                        "model": row.model,
                        "base_url": row.base_url,
                        "purpose": row.purpose,
                        "secret_ref": row.secret_ref,
                    }
                )
            for revision_attempt in range(2):
                model_attempt_no += 1
                attempt_kind = (
                    "primary"
                    if revision_index == 0 and revision_attempt == 0
                    else "retry"
                    if revision_index == 0
                    else "fallback"
                )
                call_started_at = datetime.now(timezone.utc)
                span_context = self._model_span(
                    "chat",
                    config,
                    model_attempt_no,
                    attempt_kind,
                    runtime_context,
                )
                try:
                    with span_context as model_span:
                        client_context = (
                            self._telemetry.span(
                                "provider.chat",
                                kind=SpanKind.CLIENT,
                                attributes={
                                    "client.operation": "chat",
                                    "server.address": config.provider,
                                },
                            )
                            if self._telemetry is not None
                            else nullcontext()
                        )
                        try:
                            with client_context:
                                response = self._provider_factory(config.provider).chat(
                                    config, prompt
                                )
                        except (httpx.HTTPError, ModelCallError) as exc:
                            failed_input, failed_output = _exception_usage(exc)
                            failed_cost, failed_currency = (
                                self._pricing_service.actual_cost_details(
                                    tenant_id=self._tenant_id,
                                    task_id=task_id,
                                    config=config,
                                    input_tokens=failed_input,
                                    output_tokens=failed_output,
                                    at=call_started_at,
                                )
                            )
                            if model_span is not None:
                                model_span.set_attribute(
                                    "gen_ai.usage.input_tokens", failed_input
                                )
                                model_span.set_attribute(
                                    "gen_ai.usage.output_tokens", failed_output
                                )
                                model_span.set_attribute("app.cost.amount", str(failed_cost))
                                model_span.set_attribute(
                                    "app.cost.currency", failed_currency
                                )
                            raise
                        if isinstance(response, str):
                            normalized = ProviderChatResponse(
                                content=response,
                                input_tokens=_estimated_tokens(prompt),
                                output_tokens=_estimated_tokens(response),
                            )
                        else:
                            normalized = response
                        cost, currency = self._pricing_service.actual_cost_details(
                            tenant_id=self._tenant_id,
                            task_id=task_id,
                            config=config,
                            input_tokens=normalized.input_tokens,
                            output_tokens=normalized.output_tokens,
                            at=call_started_at,
                        )
                        if model_span is not None:
                            model_span.set_attribute(
                                "gen_ai.usage.input_tokens", normalized.input_tokens
                            )
                            model_span.set_attribute(
                                "gen_ai.usage.output_tokens", normalized.output_tokens
                            )
                            model_span.set_attribute("gen_ai.response.model", config.model)
                            model_span.set_attribute("app.cost.amount", str(cost))
                            model_span.set_attribute("app.cost.currency", currency)
                    actual_usage = actual_usage.add(
                        BudgetDelta(
                            model_tokens=normalized.input_tokens + normalized.output_tokens,
                            cost=cost,
                        )
                    )
                    if op_key is not None and task_id is not None:
                        self._budget_manager.settle_effect(
                            task_id=task_id,
                            operation_key=op_key,
                            actual_usage=actual_usage,
                            result_kind="model_success",
                            result_payload={"content": normalized.content},
                        )
                    return normalized.content
                except (httpx.HTTPError, ModelCallError) as exc:
                    error_code = (
                        exc.error_code
                        if isinstance(exc, ModelCallError)
                        else _provider_error_code(exc)
                    )
                    failures.append(
                        f"{config.model_config_id}@{config.version}:{error_code}"
                    )
                    input_tokens, output_tokens = _exception_usage(exc)
                    if input_tokens or output_tokens:
                        cost, _currency = self._pricing_service.actual_cost_details(
                            tenant_id=self._tenant_id,
                            task_id=task_id,
                            config=config,
                            input_tokens=input_tokens,
                            output_tokens=output_tokens,
                            at=call_started_at,
                        )
                        actual_usage = actual_usage.add(
                            BudgetDelta(
                                model_tokens=input_tokens + output_tokens,
                                cost=cost,
                            )
                        )
        message = "Frozen model chain exhausted: " + ", ".join(failures)
        if op_key is not None and task_id is not None:
            self._budget_manager.settle_effect(
                task_id=task_id,
                operation_key=op_key,
                actual_usage=actual_usage,
                result_kind="model_error",
                result_payload={"message": message, "error_code": error_code},
            )
        raise ModelCallError(message, error_code=error_code)

    def _model_span(
        self,
        operation: str,
        config: ModelConfig,
        attempt_no: int,
        attempt_kind: str,
        runtime_context: Any,
    ) -> Any:
        if self._telemetry is None:
            return nullcontext()
        scope = TraceScope(
            tenant_id=self._tenant_id,
            user_id=(runtime_context.execution.user_id if runtime_context else config.user_id),
            task_id=(runtime_context.execution.task_id if runtime_context else None),
        )
        attributes: dict[str, Any] = {
            "gen_ai.operation.name": operation,
            "gen_ai.provider.name": config.provider,
            "gen_ai.request.model": config.model,
            "research.agent": self._role,
            "research.model_attempt_no": attempt_no,
            "research.model_attempt_kind": attempt_kind,
        }
        if runtime_context is not None:
            attributes["research.node_run_id"] = runtime_context.node_run_id
        return self._telemetry.span(
            f"gen_ai.{operation}",
            scope=scope,
            attributes=attributes,
            semantic_errors=(ValidationError,),
        )

    def invoke_structured(self, prompt: str, schema: type[T]) -> T:
        schema_contract = json.dumps(
            schema.model_json_schema(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        base_prompt = (
            f"{prompt}\n\nRequired JSON Schema:\n{schema_contract}\n"
            "Return exactly one JSON object conforming to this schema. "
            "Do not use markdown fences or explanatory text."
        )
        current_prompt = base_prompt
        last_error: Exception | None = None
        for attempt in range(2):
            raw = self.invoke_text(current_prompt)
            try:
                normalized = raw.strip()
                if normalized.startswith("```json") and normalized.endswith("```"):
                    normalized = normalized[7:-3].strip()
                elif normalized.startswith("```") and normalized.endswith("```"):
                    normalized = normalized[3:-3].strip()
                payload = json.loads(normalized)
                return schema.model_validate(payload)
            except (json.JSONDecodeError, ValidationError) as exc:
                last_error = exc
                if attempt == 0:
                    current_prompt = (
                        f"{base_prompt}\n\nYour previous response violated the required JSON schema. "
                        "Return only one corrected JSON object with no markdown."
                    )
        raise ModelCallError(
            "Model did not return schema-valid JSON after controlled retry",
            error_code="malformed_output",
        ) from last_error


class ModelRuntime:
    def __init__(
        self,
        session_factory: SessionFactory,
        provider_factory: Callable[[str], ProviderClient] | None = None,
        *,
        embedding_dimensions: int = 1024,
        telemetry: Telemetry | None = None,
    ) -> None:
        self.session_factory = session_factory
        self.embedding_dimensions = embedding_dimensions
        self.provider_factory = provider_factory or self._default_provider
        self.budget_manager = TaskBudgetManager(session_factory)
        self.pricing_service = ModelPricingService(session_factory)
        self.telemetry = telemetry

    def _default_provider(self, provider: str) -> ProviderClient:
        if provider == "fake":
            return DevelopmentFakeProviderClient()
        if provider in {"openai", "openai-compatible", "deepseek", "qwen"}:
            return OpenAICompatibleClient(
                embedding_dimensions=self.embedding_dimensions
            )
        raise ModelConfigurationError(f"Unsupported provider: {provider}")

    def build_chat_model(
        self, tenant_id: str, binding: FrozenModelBinding
    ) -> RuntimeManagedChatModel:
        return RuntimeManagedChatModel(
            self.session_factory,
            tenant_id,
            binding,
            self.provider_factory,
            self.budget_manager,
            self.pricing_service,
            binding.binding_key,
            self.telemetry,
        )

    def build_chat_model_for(
        self, tenant_id: str, binding: FrozenModelBinding, role: str
    ) -> RuntimeManagedChatModel:
        return RuntimeManagedChatModel(
            self.session_factory,
            tenant_id,
            binding,
            self.provider_factory,
            self.budget_manager,
            self.pricing_service,
            role,
            self.telemetry,
        )

    def embed(
        self, tenant_id: str, binding: FrozenModelBinding, texts: list[str]
    ) -> list[list[float]]:
        return self._invoke_vector(tenant_id, binding, "embed", texts=texts)

    def rerank(
        self,
        tenant_id: str,
        binding: FrozenModelBinding,
        query: str,
        candidates: list[str],
    ) -> list[float]:
        return self._invoke_vector(
            tenant_id, binding, "rerank", query=query, candidates=candidates
        )

    def _invoke_vector(
        self,
        tenant_id: str,
        binding: FrozenModelBinding,
        operation: str,
        **kwargs: Any,
    ) -> Any:
        failures: list[str] = []
        error_code = "model_call_failed"
        runtime_context = current_runtime_effect_context()
        attempt_no = 0
        for revision_index, revision in enumerate([binding.primary, *binding.fallback]):
            with self.session_factory() as session:
                repository = ModelConfigRepository(session, tenant_id)
                if repository.is_revoked(revision.model_config_id, revision.version):
                    failures.append(f"{revision.model_config_id}@{revision.version}:revoked")
                    continue
                row = repository.revision(revision.model_config_id, revision.version)
                if row is None:
                    failures.append(f"{revision.model_config_id}@{revision.version}:missing")
                    continue
                config = ModelConfig.model_validate(
                    {
                        "model_config_id": row.model_config_id,
                        "version": row.version,
                        "tenant_id": row.tenant_id,
                        "user_id": row.user_id,
                        "provider": row.provider,
                        "model": row.model,
                        "base_url": row.base_url,
                        "purpose": row.purpose,
                        "secret_ref": row.secret_ref,
                    }
                )
            client = self.provider_factory(config.provider)
            attempt_no += 1
            attempt_kind = "primary" if revision_index == 0 else "fallback"
            scope = TraceScope(
                tenant_id=tenant_id,
                user_id=(runtime_context.execution.user_id if runtime_context else config.user_id),
                task_id=(runtime_context.execution.task_id if runtime_context else None),
            )
            attributes: dict[str, Any] = {
                "gen_ai.operation.name": operation,
                "gen_ai.provider.name": config.provider,
                "gen_ai.request.model": config.model,
                "research.model_attempt_no": attempt_no,
                "research.model_attempt_kind": attempt_kind,
            }
            if runtime_context is not None:
                attributes["research.node_run_id"] = runtime_context.node_run_id
            model_context = (
                self.telemetry.span(
                    f"gen_ai.{operation}", scope=scope, attributes=attributes
                )
                if self.telemetry is not None
                else nullcontext()
            )
            result: list[list[float]] | list[float]
            try:
                with model_context as model_span:
                    client_context = (
                        self.telemetry.span(
                            f"provider.{operation}",
                            kind=SpanKind.CLIENT,
                            attributes={
                                "client.operation": operation,
                                "server.address": config.provider,
                            },
                        )
                        if self.telemetry is not None
                        else nullcontext()
                    )
                    with client_context:
                        if operation == "embed":
                            result = client.embed(config, kwargs["texts"])
                            input_tokens = sum(
                                _estimated_tokens(text) for text in kwargs["texts"]
                            )
                        else:
                            result = client.rerank(
                                config, kwargs["query"], kwargs["candidates"]
                            )
                            input_tokens = _estimated_tokens(kwargs["query"]) + sum(
                                _estimated_tokens(text) for text in kwargs["candidates"]
                            )
                    cost, currency = self.pricing_service.actual_cost_details(
                        tenant_id=tenant_id,
                        task_id=(runtime_context.execution.task_id if runtime_context else None),
                        config=config,
                        input_tokens=input_tokens,
                        output_tokens=0,
                        at=datetime.now(timezone.utc),
                    )
                    if model_span is not None:
                        model_span.set_attribute("gen_ai.usage.input_tokens", input_tokens)
                        model_span.set_attribute("gen_ai.usage.output_tokens", 0)
                        model_span.set_attribute("gen_ai.response.model", config.model)
                        model_span.set_attribute("app.cost.amount", str(cost))
                        model_span.set_attribute("app.cost.currency", currency)
                    return result
            except (httpx.HTTPError, ModelCallError) as exc:
                error_code = (
                    exc.error_code
                    if isinstance(exc, ModelCallError)
                    else _provider_error_code(exc)
                )
                failures.append(
                    f"{config.model_config_id}@{config.version}:{error_code}"
                )
        raise ModelCallError(
            "Frozen model chain exhausted: " + ", ".join(failures),
            error_code=error_code,
        )


class ScriptedFakeChatModel:
    def __init__(
        self,
        responses: deque[Any],
        call_log: list[str],
        binding: FrozenModelBinding,
    ) -> None:
        self._responses = responses
        self._call_log = call_log
        self._binding = binding

    @property
    def binding_identity(self) -> dict[str, Any]:
        return self._binding.model_dump(mode="json")

    def _next(self, prompt: str, schema: type[BaseModel] | None = None) -> Any:
        self._call_log.append(prompt)
        if not self._responses:
            raise AssertionError("No scripted fake model response remains")
        value = self._responses.popleft()
        if isinstance(value, Exception):
            raise value
        if callable(value):
            value = value(prompt, schema)
        return value

    def invoke_text(self, prompt: str) -> str:
        value = self._next(prompt)
        if isinstance(value, BaseModel):
            return value.model_dump_json()
        if isinstance(value, dict):
            return json.dumps(value)
        return str(value)

    def invoke_structured(self, prompt: str, schema: type[T]) -> T:
        value = self._next(prompt, schema)
        if isinstance(value, schema):
            return value
        return schema.model_validate(value)


class FakeModelRuntime:
    """Protocol-level fake used by tests; no network provider is involved."""

    def __init__(self, scripted: dict[str, list[Any]] | None = None) -> None:
        self._scripted = {key: deque(values) for key, values in (scripted or {}).items()}
        self.calls: dict[str, list[str]] = defaultdict(list)

    def build_chat_model(self, tenant_id: str, binding: FrozenModelBinding) -> ScriptedFakeChatModel:
        del tenant_id
        return ScriptedFakeChatModel(
            self._scripted.setdefault(binding.binding_key, deque()),
            self.calls[binding.binding_key],
            binding,
        )

    def build_chat_model_for(
        self, tenant_id: str, binding: FrozenModelBinding, role: str
    ) -> ScriptedFakeChatModel:
        del tenant_id
        role_key = f"{binding.binding_key}:{role}"
        key = role_key if role_key in self._scripted else binding.binding_key
        return ScriptedFakeChatModel(
            self._scripted.setdefault(key, deque()),
            self.calls[key],
            binding,
        )

    def embed(
        self, tenant_id: str, binding: FrozenModelBinding, texts: list[str]
    ) -> list[list[float]]:
        del tenant_id, binding
        return [_deterministic_vector(text) for text in texts]

    def rerank(
        self,
        tenant_id: str,
        binding: FrozenModelBinding,
        query: str,
        candidates: list[str],
    ) -> list[float]:
        del tenant_id, binding
        query_terms = set(query.lower().split())
        return [float(len(query_terms & set(item.lower().split()))) for item in candidates]


def _deterministic_vector(text: str, dimension: int = 32) -> list[float]:
    import hashlib

    digest = hashlib.sha256(text.encode("utf-8")).digest()
    return [((digest[index % len(digest)] / 255.0) * 2.0) - 1.0 for index in range(dimension)]


def _estimated_tokens(text: str) -> int:
    return max(1, (len(text) + 3) // 4)


def _exception_usage(exc: BaseException) -> tuple[int, int]:
    usage = getattr(exc, "usage", None)
    if isinstance(usage, dict):
        return int(usage.get("input_tokens") or 0), int(usage.get("output_tokens") or 0)
    return int(getattr(exc, "input_tokens", 0) or 0), int(
        getattr(exc, "output_tokens", 0) or 0
    )
