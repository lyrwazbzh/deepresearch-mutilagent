from __future__ import annotations

import pytest

from src.agent.tools.web import JinaReaderProvider, JinaSearchProvider
from src.agent.config import MissingRealProviderSecretError, Settings
from src.agent.schema.model_config import ModelConfig
from src.agent.llm.runtime import OpenAICompatibleClient


def settings() -> Settings:
    return Settings(
        database_url="sqlite://",
        redis_url="redis://localhost/0",
        celery_broker_url="redis://localhost/1",
        default_tenant_id="tenant",
        default_user_id="user",
        model_mode="configured",
    )


@pytest.mark.parametrize("placeholder", ["xxxx", "xxxxxx"])
def test_real_provider_secret_validation_blocks_empty_and_placeholder(
    monkeypatch, placeholder
) -> None:
    for name in (
        "OPENAI_API_KEY",
        "OPENAI_EMBEDDING_API_KEY",
        "TAVILY_API_KEY",
        "JINA_API_KEY",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", placeholder)

    configured = settings()
    assert configured.missing_real_provider_secrets() == (
        "OPENAI_API_KEY",
        "OPENAI_EMBEDDING_API_KEY",
        "TAVILY_API_KEY",
        "JINA_API_KEY",
    )
    with pytest.raises(
        MissingRealProviderSecretError, match="BLOCKED_MISSING_REAL_SECRET"
    ):
        configured.require_real_provider_secrets()

    for name in (
        "OPENAI_API_KEY",
        "OPENAI_EMBEDDING_API_KEY",
        "TAVILY_API_KEY",
        "JINA_API_KEY",
    ):
        monkeypatch.setenv(name, f"test-{name.lower()}")
    configured.require_real_provider_secrets()


def test_openai_embedding_request_uses_configured_dimension(monkeypatch) -> None:
    captured: dict[str, object] = {}

    class Response:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, object]:
            return {"data": [{"embedding": [0.1, 0.2]}]}

    def post(url, *, headers, json, timeout):
        captured.update(url=url, headers=headers, json=json, timeout=timeout)
        return Response()

    monkeypatch.setenv("TEST_OPENAI_KEY", "test-secret")
    monkeypatch.setattr("src.agent.llm.runtime.httpx.post", post)
    config = ModelConfig(
        model_config_id="embedding",
        version=1,
        tenant_id="tenant",
        user_id="user",
        provider="openai",
        model="text-embedding-3-small",
        base_url="https://api.openai.com/v1",
        purpose="embedding",
        secret_ref="TEST_OPENAI_KEY",
    )

    result = OpenAICompatibleClient(embedding_dimensions=1024).embed(config, ["text"])

    assert result == [[0.1, 0.2]]
    assert captured["json"] == {
        "model": "text-embedding-3-small",
        "input": ["text"],
        "dimensions": 1024,
    }


def test_jina_adapters_apply_configured_credential() -> None:
    calls: list[dict[str, object]] = []

    class Response:
        text = "Title: Example\nBody"

        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, object]:
            return {"data": [{"title": "Example", "url": "https://example.test"}]}

    class Client:
        def get(self, url, **kwargs):
            calls.append({"url": url, **kwargs})
            return Response()

    client = Client()
    search = JinaSearchProvider(api_key="test-jina", client=client)
    reader = JinaReaderProvider(api_key="test-jina", client=client)

    assert search.search("phase 6", max_results=1)[0].title == "Example"
    assert reader.read("https://example.test")[1] == "Body"
    assert all(
        call["headers"]["Authorization"] == "Bearer test-jina"  # type: ignore[index]
        for call in calls
    )
