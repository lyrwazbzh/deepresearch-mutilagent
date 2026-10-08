from __future__ import annotations

from src.agent.config import Settings
from src.agent.llm.runtime import PHASE1_BINDINGS
from src.agent.backends.persistence.database import SessionFactory, create_session_factory
from src.agent.backends.persistence.models import ModelBindingRow, ModelConfigHeadRow, ModelConfigRow


def seed_fake_models(settings: Settings | None = None) -> None:
    settings = settings or Settings.from_env()
    if settings.model_mode != "fake":
        raise RuntimeError("Refusing to seed a fake provider unless DEEPRESEARCH_MODEL_MODE=fake")
    session_factory = create_session_factory(settings.database_url)
    config_id = "model_fake_chat"
    with session_factory() as session, session.begin():
        config = session.get(ModelConfigRow, (config_id, 1))
        if config is None:
            session.add(
                ModelConfigRow(
                    model_config_id=config_id,
                    version=1,
                    tenant_id=settings.default_tenant_id,
                    user_id=settings.default_user_id,
                    provider="fake",
                    model="deterministic-phase1",
                    purpose="chat",
                )
            )
            session.flush()
        head = session.get(ModelConfigHeadRow, (settings.default_tenant_id, config_id))
        if head is None:
            session.add(
                ModelConfigHeadRow(
                    tenant_id=settings.default_tenant_id,
                    user_id=settings.default_user_id,
                    model_config_id=config_id,
                    current_version=1,
                    enabled_for_new_tasks=True,
                )
            )
            session.flush()
        for key in PHASE1_BINDINGS:
            identity = (settings.default_tenant_id, key, "chat", 0)
            if session.get(ModelBindingRow, identity) is None:
                session.add(
                    ModelBindingRow(
                        tenant_id=settings.default_tenant_id,
                        binding_key=key,
                        purpose="chat",
                        priority=0,
                        model_config_id=config_id,
                    )
                )
        for key, purpose in (("embedding", "embedding"), ("rerank", "rerank")):
            purpose_config_id = f"model_fake_{purpose}"
            if session.get(ModelConfigRow, (purpose_config_id, 1)) is None:
                session.add(
                    ModelConfigRow(
                        model_config_id=purpose_config_id,
                        version=1,
                        tenant_id=settings.default_tenant_id,
                        user_id=settings.default_user_id,
                        provider="fake",
                        model=f"deterministic-{purpose}",
                        purpose=purpose,
                    )
                )
                session.flush()
            if session.get(
                ModelConfigHeadRow, (settings.default_tenant_id, purpose_config_id)
            ) is None:
                session.add(
                    ModelConfigHeadRow(
                        tenant_id=settings.default_tenant_id,
                        user_id=settings.default_user_id,
                        model_config_id=purpose_config_id,
                        current_version=1,
                        enabled_for_new_tasks=True,
                    )
                )
                session.flush()
            identity = (settings.default_tenant_id, key, purpose, 0)
            if session.get(ModelBindingRow, identity) is None:
                session.add(
                    ModelBindingRow(
                        tenant_id=settings.default_tenant_id,
                        binding_key=key,
                        purpose=purpose,
                        priority=0,
                        model_config_id=purpose_config_id,
                    )
                )


def seed_configured_models(
    settings: Settings | None = None,
    *,
    session_factory: SessionFactory | None = None,
) -> None:
    settings = settings or Settings.from_env()
    if settings.model_mode != "configured":
        raise RuntimeError(
            "Refusing to seed real providers unless DEEPRESEARCH_MODEL_MODE=configured"
        )
    settings.require_real_provider_secrets()
    sessions = session_factory or create_session_factory(settings.database_url)
    configs = (
        (
            "model_openai_chat",
            settings.openai_chat_provider,
            settings.openai_chat_model,
            settings.openai_base_url,
            "chat",
            settings.openai_secret_ref,
            PHASE1_BINDINGS,
        ),
        (
            "model_openai_embedding",
            settings.openai_embedding_provider,
            settings.openai_embedding_model,
            settings.openai_embedding_base_url,
            "embedding",
            settings.openai_embedding_secret_ref,
            ("embedding",),
        ),
        (
            "model_jina_rerank",
            "openai-compatible",
            settings.jina_rerank_model,
            settings.jina_base_url,
            "rerank",
            settings.jina_secret_ref,
            ("rerank",),
        ),
    )
    with sessions() as session, session.begin():
        for (
            config_id,
            provider,
            model,
            base_url,
            purpose,
            secret_ref,
            binding_keys,
        ) in configs:
            existing = session.get(ModelConfigRow, (config_id, 1))
            expected = {
                "tenant_id": settings.default_tenant_id,
                "user_id": settings.default_user_id,
                "provider": provider,
                "model": model,
                "base_url": base_url,
                "purpose": purpose,
                "secret_ref": secret_ref,
            }
            if existing is None:
                session.add(
                    ModelConfigRow(
                        model_config_id=config_id,
                        version=1,
                        **expected,
                    )
                )
                session.flush()
            elif any(getattr(existing, key) != value for key, value in expected.items()):
                raise RuntimeError(
                    f"Configured ModelConfig {config_id}@1 differs from the requested settings; "
                    "create a new immutable revision instead of overwriting it"
                )
            head = session.get(ModelConfigHeadRow, (settings.default_tenant_id, config_id))
            if head is None:
                session.add(
                    ModelConfigHeadRow(
                        tenant_id=settings.default_tenant_id,
                        user_id=settings.default_user_id,
                        model_config_id=config_id,
                        current_version=1,
                        enabled_for_new_tasks=True,
                    )
                )
            else:
                head.current_version = 1
                head.enabled_for_new_tasks = True
            for binding_key in binding_keys:
                identity = (settings.default_tenant_id, binding_key, purpose, 0)
                binding = session.get(ModelBindingRow, identity)
                if binding is None:
                    session.add(
                        ModelBindingRow(
                            tenant_id=settings.default_tenant_id,
                            binding_key=binding_key,
                            purpose=purpose,
                            priority=0,
                            model_config_id=config_id,
                        )
                    )
                else:
                    binding.model_config_id = config_id


def seed_models() -> None:
    settings = Settings.from_env()
    if settings.model_mode == "fake":
        seed_fake_models(settings)
        return
    if settings.model_mode == "configured":
        seed_configured_models(settings)
        return
    raise RuntimeError(f"Unsupported DEEPRESEARCH_MODEL_MODE={settings.model_mode!r}")


if __name__ == "__main__":
    seed_models()
