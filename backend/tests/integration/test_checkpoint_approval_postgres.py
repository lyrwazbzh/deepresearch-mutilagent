import os
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.orm import sessionmaker

import models.chat  # noqa: F401 - register relationship targets
import models.knowledge  # noqa: F401 - register relationship targets
from models.research import ResearchCheckpoint
from models.user import User
from service.checkpoint_service import (
    CheckpointService,
    OutlineApprovalConflict,
)


pytestmark = [pytest.mark.integration, pytest.mark.needs_infra]
TEST_DATABASE_URL = os.getenv("TEST_DATABASE_URL", "")
MIGRATION_PATH = (
    Path(__file__).parents[2]
    / "migrations"
    / "20260719_unique_research_checkpoint_session_id.sql"
)


def _postgres_url():
    if not TEST_DATABASE_URL.startswith(("postgresql://", "postgresql+")):
        pytest.skip("TEST_DATABASE_URL must point to an isolated PostgreSQL database")
    return TEST_DATABASE_URL


@pytest.fixture
def isolated_schema():
    database_url = _postgres_url()
    schema = f"outline_approval_test_{uuid.uuid4().hex}"
    admin_engine = create_engine(database_url)
    with admin_engine.begin() as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))

    schema_engine = create_engine(
        database_url,
        connect_args={"options": f"-csearch_path={schema}"},
    )
    try:
        yield schema_engine
    finally:
        schema_engine.dispose()
        with admin_engine.begin() as connection:
            connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin_engine.dispose()


def _sections():
    return [
        {
            "id": f"section_{index}",
            "title": f"Chapter {index}",
            "description": f"Description {index}",
        }
        for index in range(1, 4)
    ]


def _questions():
    return [
        {"id": f"question_{index}", "text": f"Question {index}"}
        for index in range(1, 4)
    ]


def test_concurrent_approval_has_exactly_one_winner(isolated_schema):
    User.__table__.create(isolated_schema)
    ResearchCheckpoint.__table__.create(isolated_schema)
    user_id = uuid.uuid4()
    session_factory = sessionmaker(bind=isolated_schema)

    with isolated_schema.begin() as connection:
        connection.execute(
            User.__table__.insert().values(
                id=user_id,
                username=f"user-{user_id.hex}",
                email=f"{user_id.hex}@example.com",
                hashed_password="test",
            )
        )
        connection.execute(
            ResearchCheckpoint.__table__.insert().values(
                id=uuid.uuid4(),
                session_id="session-1",
                user_id=user_id,
                query="Industry research",
                phase="awaiting_outline_approval",
                iteration=0,
                status="paused",
                state_json={
                    "query": "Industry research",
                    "phase": "awaiting_outline_approval",
                    "outline_revision": "revision-1",
                },
            )
        )

    def approve():
        service = CheckpointService()
        service._get_db = session_factory
        try:
            service.approve_outline(
                "session-1",
                str(user_id),
                "revision-1",
                _sections(),
                _questions(),
            )
            return "approved"
        except OutlineApprovalConflict:
            return "conflict"

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _index: approve(), range(2)))

    assert sorted(results) == ["approved", "conflict"]


def test_unique_constraint_rejects_duplicate_sessions(isolated_schema):
    User.__table__.create(isolated_schema)
    ResearchCheckpoint.__table__.create(isolated_schema)
    user_id = uuid.uuid4()
    with isolated_schema.begin() as connection:
        connection.execute(
            User.__table__.insert().values(
                id=user_id,
                username=f"user-{user_id.hex}",
                email=f"{user_id.hex}@example.com",
                hashed_password="test",
            )
        )
        values = {
            "user_id": user_id,
            "session_id": "duplicate-session",
            "query": "Research",
            "phase": "planning",
            "iteration": 0,
            "status": "running",
            "state_json": {},
        }
        connection.execute(
            ResearchCheckpoint.__table__.insert().values(id=uuid.uuid4(), **values)
        )

    with pytest.raises(IntegrityError):
        with isolated_schema.begin() as connection:
            connection.execute(
                ResearchCheckpoint.__table__.insert().values(
                    id=uuid.uuid4(),
                    **values,
                )
            )


def test_migration_aborts_on_duplicates_without_deleting_rows(isolated_schema):
    with isolated_schema.begin() as connection:
        connection.execute(
            text("CREATE TABLE research_checkpoints (session_id VARCHAR(64) NOT NULL)")
        )
        connection.execute(
            text(
                "INSERT INTO research_checkpoints (session_id) "
                "VALUES ('duplicate'), ('duplicate')"
            )
        )

    migration_sql = MIGRATION_PATH.read_text(encoding="utf-8")
    with pytest.raises(DBAPIError):
        with isolated_schema.begin() as connection:
            connection.exec_driver_sql(migration_sql)

    with isolated_schema.connect() as connection:
        row_count = connection.scalar(
            text("SELECT COUNT(*) FROM research_checkpoints")
        )
    assert row_count == 2


def test_migration_creates_unique_index_for_clean_rows(isolated_schema):
    with isolated_schema.begin() as connection:
        connection.execute(
            text("CREATE TABLE research_checkpoints (session_id VARCHAR(64) NOT NULL)")
        )
        connection.execute(
            text(
                "INSERT INTO research_checkpoints (session_id) "
                "VALUES ('session-1'), ('session-2')"
            )
        )
        connection.exec_driver_sql(MIGRATION_PATH.read_text(encoding="utf-8"))

    with isolated_schema.connect() as connection:
        index_exists = connection.scalar(
            text(
                "SELECT EXISTS ("
                "SELECT 1 FROM pg_indexes "
                "WHERE schemaname = current_schema() "
                "AND indexname = 'uq_research_checkpoints_session_id'"
                ")"
            )
        )
    assert index_exists is True
