from __future__ import annotations

from src.agent.bootstrap import seed_configured_models
from src.agent.config import Settings
from src.agent.llm.runtime import PHASE2_BINDINGS, ModelResolver
from src.agent.backends.persistence.models import ModelConfigRow


def test_configured_bootstrap_seeds_frozen_real_provider_bindings(
    session_factory, monkeypatch
) -> None:
    for name in (
        "OPENAI_API_KEY",
        "OPENAI_EMBEDDING_API_KEY",
        "TAVILY_API_KEY",
        "JINA_API_KEY",
    ):
        monkeypatch.setenv(name, f"test-{name.lower()}")
    configured = Settings(
        database_url=str(session_factory.kw["bind"].url),
        redis_url="redis://localhost/0",
        celery_broker_url="redis://localhost/1",
        default_tenant_id="tenant_real",
        default_user_id="user_real",
        model_mode="configured",
    )

    seed_configured_models(configured)

    with session_factory() as session:
        snapshot = ModelResolver().resolve_snapshot(
            session, "tenant_real", required_keys=PHASE2_BINDINGS
        )
        chat = session.get(ModelConfigRow, ("model_openai_chat", 1))
        embedding = session.get(ModelConfigRow, ("model_openai_embedding", 1))
        rerank = session.get(ModelConfigRow, ("model_jina_rerank", 1))
        assert snapshot.get("routing").primary.model_config_id == "model_openai_chat"
        assert snapshot.get("embedding").primary.model_config_id == "model_openai_embedding"
        assert snapshot.get("rerank").primary.model_config_id == "model_jina_rerank"
        assert chat.secret_ref == "OPENAI_API_KEY"
        assert embedding.secret_ref == "OPENAI_EMBEDDING_API_KEY"
        assert rerank.secret_ref == "JINA_API_KEY"
        assert "test-openai_api_key" not in repr((chat, embedding, rerank))
