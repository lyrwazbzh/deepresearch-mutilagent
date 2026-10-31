from __future__ import annotations

from uuid import uuid4

import pytest
from sqlalchemy.orm import Session

from core.database import engine
from models.chat import ChatSession
from models.industry_data import CompanyData, IndustryStats, PolicyData  # noqa: F401
from models.knowledge import Document, KnowledgeBase  # noqa: F401
from models.news import BiddingInfo, IndustryNews, NewsCollectionTask  # noqa: F401
from models.research import ResearchCheckpoint  # noqa: F401
from models.user import User
from observability.events import sanitize_event_payload
from service.research_observability_service import ResearchObservabilityService


def test_event_payload_sanitizer_summarizes_content_and_redacts_secrets():
    payload = {
        "phase": "researching",
        "prompt": "find private market evidence",
        "nested": {"api_key": "secret", "documents": ["one", "two"]},
        "result_count": 2,
    }

    sanitized = sanitize_event_payload(payload)

    assert sanitized["phase"] == "researching"
    assert sanitized["result_count"] == 2
    assert sanitized["prompt"]["redacted"] is True
    assert sanitized["prompt"]["length"] == len(payload["prompt"])
    assert len(sanitized["prompt"]["sha256"]) == 64
    assert sanitized["nested"]["api_key"] == "[REDACTED]"
    assert sanitized["nested"]["documents"]["count"] == 2


def test_event_persistence_failure_is_degraded_by_default():
    def broken_session_factory():
        raise RuntimeError("database unavailable")

    service = ResearchObservabilityService(session_factory=broken_session_factory)

    assert service.record_event(run_id=str(uuid4()), event_type="phase.started") is None


def test_finish_run_rejects_non_terminal_status():
    service = ResearchObservabilityService()

    with pytest.raises(ValueError, match="Invalid terminal research status"):
        service.finish_run(run_id=str(uuid4()), status="running")


@pytest.mark.integration
@pytest.mark.needs_infra
def test_run_event_lifecycle_sequence_pagination_and_user_scope():
    from models.observability import ResearchEvent, ResearchRun

    ResearchRun.__table__.create(bind=engine, checkfirst=True)
    ResearchEvent.__table__.create(bind=engine, checkfirst=True)

    connection = engine.connect()
    outer_transaction = connection.begin()
    setup_session = Session(bind=connection, join_transaction_mode="create_savepoint")
    try:
        first_user = User(
            username=f"observer-{uuid4().hex}",
            email=f"observer-{uuid4().hex}@example.com",
            hashed_password="not-used",
        )
        second_user = User(
            username=f"other-{uuid4().hex}",
            email=f"other-{uuid4().hex}@example.com",
            hashed_password="not-used",
        )
        setup_session.add_all([first_user, second_user])
        setup_session.flush()
        chat_session = ChatSession(user_id=first_user.id, title="observability test")
        setup_session.add(chat_session)
        setup_session.commit()

        service = ResearchObservabilityService(
            session_factory=lambda: Session(
                bind=connection,
                join_transaction_mode="create_savepoint",
            )
        )
        run = service.start_run(
            session_id=str(chat_session.id),
            user_id=str(first_user.id),
            request_id="req-integration",
            query="research a private topic",
        )
        assert run["status"] == "running"
        assert run["research_id"]

        first_event = service.record_event(
            run_id=run["run_id"],
            event_type="phase.started",
            phase="researching",
            payload={"query": "research a private topic"},
        )
        second_event = service.record_event(
            run_id=run["run_id"],
            event_type="retrieval.completed",
            phase="researching",
            payload={"result_count": 4},
        )
        assert first_event is not None and first_event["sequence"] == 1
        assert second_event is not None and second_event["sequence"] == 2

        first_page = service.list_events(
            run_id=run["run_id"],
            user_id=str(first_user.id),
            limit=1,
        )
        assert len(first_page["items"]) == 1
        assert first_page["next_cursor"] == 1
        second_page = service.list_events(
            run_id=run["run_id"],
            user_id=str(first_user.id),
            after_sequence=first_page["next_cursor"],
            limit=10,
        )
        assert [item["sequence"] for item in second_page["items"]] == [2]

        service.finish_run(
            run_id=run["run_id"],
            status="completed",
            input_tokens=100,
            output_tokens=50,
        )
        completed = service.get_run(run["run_id"], user_id=str(first_user.id))
        assert completed is not None
        assert completed["status"] == "completed"
        assert completed["duration_ms"] is not None
        assert completed["input_tokens"] == 100
        assert completed["output_tokens"] == 50

        assert service.get_run(run["run_id"], user_id=str(second_user.id)) is None
        assert service.list_runs(
            session_id=str(chat_session.id),
            user_id=str(second_user.id),
        )["items"] == []
        timeline = service.get_timeline(
            session_id=str(chat_session.id),
            user_id=str(first_user.id),
        )
        assert timeline is not None
        assert [event["sequence"] for event in timeline["events"]] == [1, 2]
        assert service.get_timeline(
            session_id=str(chat_session.id),
            user_id=str(second_user.id),
        ) is None

        repeated_research = service.start_run(
            session_id=str(chat_session.id),
            user_id=str(first_user.id),
            research_id=run["research_id"],
            query="retry",
        )
        assert repeated_research["research_id"] == run["research_id"]
        assert repeated_research["run_id"] != run["run_id"]
    finally:
        setup_session.close()
        outer_transaction.rollback()
        connection.close()
