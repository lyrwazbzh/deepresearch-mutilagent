from __future__ import annotations

import os
from collections.abc import Callable

import pytest

from src.agent.config import Settings
from src.agent.container import build_container
from src.agent.backends.events import InMemoryEventBroker
from src.agent.llm.runtime import FakeModelRuntime, PHASE1_BINDINGS
from src.agent.backends.persistence.database import create_session_factory
from src.agent.backends.persistence.models import (
    Base,
    ModelBindingRow,
    ModelConfigHeadRow,
    ModelConfigRow,
)


def seed_bindings(session_factory: Callable) -> None:
    with session_factory() as session, session.begin():
        session.add(
            ModelConfigRow(
                model_config_id="model_test",
                version=1,
                tenant_id="tenant_test",
                user_id="user_test",
                provider="fake",
                model="test",
                purpose="chat",
            )
        )
        session.add(
            ModelConfigHeadRow(
                tenant_id="tenant_test",
                user_id="user_test",
                model_config_id="model_test",
                current_version=1,
                enabled_for_new_tasks=True,
            )
        )
        session.flush()
        for key in PHASE1_BINDINGS:
            session.add(
                ModelBindingRow(
                    tenant_id="tenant_test",
                    binding_key=key,
                    purpose="chat",
                    priority=0,
                    model_config_id="model_test",
                )
            )
        for key, purpose in (("embedding", "embedding"), ("rerank", "rerank")):
            config_id = f"model_test_{purpose}"
            session.add(
                ModelConfigRow(
                    model_config_id=config_id,
                    version=1,
                    tenant_id="tenant_test",
                    user_id="user_test",
                    provider="fake",
                    model="test",
                    purpose=purpose,
                )
            )
            session.add(
                ModelConfigHeadRow(
                    tenant_id="tenant_test",
                    user_id="user_test",
                    model_config_id=config_id,
                    current_version=1,
                    enabled_for_new_tasks=True,
                )
            )
            session.flush()
            session.add(
                ModelBindingRow(
                    tenant_id="tenant_test",
                    binding_key=key,
                    purpose=purpose,
                    priority=0,
                    model_config_id=config_id,
                )
            )


@pytest.fixture
def session_factory(tmp_path):
    database_url = os.getenv("TEST_DATABASE_URL")
    if database_url:
        from sqlalchemy.engine import make_url

        database_name = make_url(database_url).database or ""
        if "test" not in database_name.lower():
            raise RuntimeError("TEST_DATABASE_URL must point to a dedicated database containing 'test'")
    else:
        database_url = f"sqlite:///{tmp_path / 'test.db'}"
    factory = create_session_factory(database_url)
    engine = factory.kw["bind"]
    if database_url.startswith("postgresql"):
        Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    seed_bindings(factory)
    yield factory
    if database_url.startswith("postgresql"):
        Base.metadata.drop_all(engine)
    engine.dispose()


@pytest.fixture
def make_container(session_factory):
    def make(scripted: dict[str, list[object]]):
        settings = Settings(
            database_url="sqlite://",
            redis_url="redis://unused",
            celery_broker_url="redis://unused",
            default_tenant_id="tenant_test",
            default_user_id="user_test",
            model_mode="fake",
            lease_seconds=300,
        )
        runtime = FakeModelRuntime(scripted)
        container = build_container(
            settings,
            session_factory=session_factory,
            model_runtime=runtime,
            event_broker=InMemoryEventBroker(),
        )
        return container, runtime

    return make
