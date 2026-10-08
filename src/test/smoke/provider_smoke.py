from __future__ import annotations

import json
import sys
import tempfile
import time
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Literal

import httpx
from pydantic import BaseModel, ConfigDict

from src.agent.bootstrap import seed_configured_models
from src.agent.tools.web import (
    ArxivSearchProvider,
    JinaReaderProvider,
    SearchWebAdapter,
    TavilySearchProvider,
)
from src.agent.config import MissingRealProviderSecretError, Settings
from src.agent.llm.runtime import PHASE2_BINDINGS, ModelCallError, ModelResolver, ModelRuntime
from src.agent.backends.persistence.database import create_session_factory
from src.agent.backends.persistence.models import Base
from src.agent.runtime.tool_runtime import ToolExecutionContext


class StructuredSmokeResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: Literal["provider-ok"]


@dataclass(frozen=True)
class SmokeResult:
    name: str
    status: Literal["PASS", "FAIL", "NOT_CONFIGURED"]
    provider: str
    model: str | None
    latency_ms: int
    dimension: int | None = None
    error_code: str | None = None


class _NeverCancelled:
    def is_cancelled(self) -> bool:
        return False


def _web_context() -> ToolExecutionContext:
    return ToolExecutionContext(
        tenant_id="provider_smoke",
        user_id="provider_smoke",
        task_id="provider_smoke",
        thread_id="provider_smoke",
        node_run_id="provider_smoke",
        cancellation_token=_NeverCancelled(),
        deadline=datetime.now(timezone.utc) + timedelta(seconds=30),
        inline_budget=4_096,
        operation_key="0" * 64,
    )


def _error_code(exc: Exception) -> str:
    if isinstance(exc, MissingRealProviderSecretError):
        return "BLOCKED_MISSING_REAL_SECRET"
    if isinstance(exc, httpx.TimeoutException):
        return "timeout"
    if isinstance(exc, ModelCallError):
        return exc.error_code
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        if status in {401, 403}:
            return "authentication_failed"
        if status == 404:
            return "model_or_endpoint_not_found"
        if status == 429:
            return "rate_limited"
        return f"provider_http_{status}"
    return type(exc).__name__


def _measure(
    name: str,
    provider: str,
    model: str | None,
    call,
    *,
    dimension: int | None = None,
) -> tuple[SmokeResult, object | None]:
    started = time.perf_counter()
    try:
        value = call()
        return (
            SmokeResult(
                name=name,
                status="PASS",
                provider=provider,
                model=model,
                latency_ms=round((time.perf_counter() - started) * 1000),
                dimension=dimension,
            ),
            value,
        )
    except Exception as exc:  # smoke output is intentionally sanitized
        return (
            SmokeResult(
                name=name,
                status="FAIL",
                provider=provider,
                model=model,
                latency_ms=round((time.perf_counter() - started) * 1000),
                dimension=dimension,
                error_code=_error_code(exc),
            ),
            None,
        )


def run_web(settings: Settings) -> list[SmokeResult]:
    results: list[SmokeResult] = []
    context = _web_context()
    tavily = TavilySearchProvider(
        api_key=settings.resolve_secret(settings.tavily_secret_ref)
    )
    arxiv = ArxivSearchProvider()
    search = SearchWebAdapter([tavily], [arxiv])
    result, value = _measure(
        "Tavily",
        "tavily",
        None,
        lambda: search.execute(
            context, {"query": "OpenAI research", "source": "web", "max_results": 1}
        ),
    )
    if result.status == "PASS" and not value.data["hits"]:  # type: ignore[union-attr]
        result = replace(result, status="FAIL", error_code="empty_result")
    results.append(result)

    result, value = _measure(
        "arXiv",
        "arxiv",
        None,
        lambda: search.execute(
            context,
            {"query": "large language models", "source": "paper", "max_results": 1},
        ),
    )
    if result.status == "PASS" and not value.data["hits"]:  # type: ignore[union-attr]
        result = replace(result, status="FAIL", error_code="empty_result")
    results.append(result)

    jina = JinaReaderProvider(api_key=settings.resolve_secret(settings.jina_secret_ref))
    result, value = _measure(
        "Jina",
        "jina-reader",
        None,
        lambda: jina.read("https://example.com"),
    )
    if result.status == "PASS" and not value[1].strip():  # type: ignore[index]
        result = replace(result, status="FAIL", error_code="empty_result")
    results.append(result)
    return results


def run() -> list[SmokeResult]:
    settings = Settings.from_env()
    settings.require_real_provider_secrets()
    results: list[SmokeResult] = []

    with tempfile.TemporaryDirectory(prefix="deepresearch-provider-smoke-") as temporary:
        database_url = f"sqlite:///{Path(temporary) / 'smoke.db'}"
        smoke_settings = replace(settings, database_url=database_url)
        session_factory = create_session_factory(database_url)
        Base.metadata.create_all(session_factory.kw["bind"])
        seed_configured_models(smoke_settings, session_factory=session_factory)

        with session_factory() as session:
            snapshot = ModelResolver().resolve_snapshot(
                session,
                settings.default_tenant_id,
                required_keys=PHASE2_BINDINGS,
            )
        runtime = ModelRuntime(
            session_factory,
            embedding_dimensions=settings.openai_embedding_dimensions,
        )

        chat = runtime.build_chat_model(
            settings.default_tenant_id, snapshot.get("routing")
        )
        result, value = _measure(
            "LLM chat",
            settings.openai_chat_provider,
            settings.openai_chat_model,
            lambda: chat.invoke_text("Reply with exactly: provider-ok"),
        )
        if result.status == "PASS" and str(value).strip() != "provider-ok":
            result = replace(result, status="FAIL", error_code="unexpected_response")
        results.append(result)

        structured = runtime.build_chat_model(
            settings.default_tenant_id, snapshot.get("reasoning")
        )
        result, _ = _measure(
            "Structured output",
            settings.openai_chat_provider,
            settings.openai_chat_model,
            lambda: structured.invoke_structured(
                'Return only this JSON object: {"status":"provider-ok"}',
                StructuredSmokeResult,
            ),
        )
        results.append(result)

        result, vectors = _measure(
            "Embedding",
            settings.openai_embedding_provider,
            settings.openai_embedding_model,
            lambda: runtime.embed(
                settings.default_tenant_id,
                snapshot.get("embedding"),
                ["phase6 embedding smoke test"],
            ),
        )
        if result.status == "PASS":
            actual_dimension = len(vectors[0])  # type: ignore[index]
            result = replace(result, dimension=actual_dimension)
            if actual_dimension != settings.openai_embedding_dimensions:
                result = replace(
                    result,
                    status="FAIL",
                    error_code="embedding_dimension_mismatch",
                )
        results.append(result)
        session_factory.kw["bind"].dispose()

    results.extend(run_web(settings))
    return results


def main() -> int:
    try:
        if sys.argv[1:] == ["--web-only"]:
            settings = Settings.from_env()
            settings.require_real_provider_secrets()
            results = run_web(settings)
        else:
            results = run()
    except Exception as exc:
        results = [
            SmokeResult(
                name="Provider configuration",
                status="FAIL",
                provider="configuration",
                model=None,
                latency_ms=0,
                error_code=_error_code(exc),
            )
        ]
    print(json.dumps([asdict(item) for item in results], ensure_ascii=False, indent=2))
    return 0 if all(item.status == "PASS" for item in results) else 1


if __name__ == "__main__":
    sys.exit(main())
