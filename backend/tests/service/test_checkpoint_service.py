import asyncio
import json
from copy import deepcopy
from datetime import datetime, timezone
from uuid import UUID

import pytest

from models.research import ResearchCheckpoint
from service.checkpoint_service import CheckpointService


USER_ID = "11111111-1111-1111-1111-111111111111"


def _sections(count: int = 3):
    return [
        {
            "id": f"section_{index}",
            "title": f"章节 {index}",
            "description": f"描述 {index}",
            "section_type": "mixed",
            "requires_data": False,
            "requires_chart": False,
        }
        for index in range(1, count + 1)
    ]


def _questions(count: int = 3):
    return [
        {"id": f"question_{index}", "text": f"问题 {index}"}
        for index in range(1, count + 1)
    ]


class FakeCheckpoint:
    def __init__(self):
        self.id = UUID("22222222-2222-2222-2222-222222222222")
        self.session_id = "session-1"
        self.user_id = UUID(USER_ID)
        self.phase = "awaiting_outline_approval"
        self.status = "paused"
        self.state_json = {
            "query": "产业研究",
            "phase": "awaiting_outline_approval",
            "outline_revision": "revision-1",
            "outline": [],
            "research_questions": [],
        }
        self.updated_at = None


class FakeQuery:
    def __init__(self, row):
        self.row = row
        self.locked = False

    def filter(self, *_conditions):
        return self

    def with_for_update(self):
        self.locked = True
        return self

    def first(self):
        return self.row


class FakeSession:
    def __init__(self, row):
        self.query_result = FakeQuery(row)
        self.committed = False
        self.rolled_back = False
        self.closed = False

    def query(self, _model):
        return self.query_result

    def commit(self):
        self.committed = True

    def rollback(self):
        self.rolled_back = True

    def close(self):
        self.closed = True


def _service_with_row(row):
    session = FakeSession(row)
    service = CheckpointService()
    service._get_db = lambda: session
    return service, session


def _assert_exception_name(expected_name, action):
    with pytest.raises(BaseException) as caught:
        action()
    assert type(caught.value).__name__ == expected_name


def test_approve_outline_locks_and_transitions_checkpoint():
    row = FakeCheckpoint()
    service, session = _service_with_row(row)

    result = service.approve_outline(
        session_id="session-1",
        user_id=USER_ID,
        outline_revision="revision-1",
        sections=_sections(),
        research_questions=_questions(),
    )

    assert session.query_result.locked is True
    assert session.committed is True
    assert session.closed is True
    assert row.phase == "planning"
    assert row.status == "running"
    assert row.state_json["phase"] == "planning"
    assert [section["id"] for section in row.state_json["outline"]] == [
        "section_1",
        "section_2",
        "section_3",
    ]
    assert all(
        section["status"] == "pending"
        and section["search_queries"] == []
        for section in row.state_json["outline"]
    )
    assert row.state_json["research_questions"] == _questions()
    assert result == deepcopy(row.state_json)


def test_approve_outline_hides_missing_or_foreign_checkpoint():
    service, session = _service_with_row(None)

    _assert_exception_name(
        "CheckpointNotFound",
        lambda: service.approve_outline(
            "session-1", USER_ID, "revision-1", _sections(), _questions()
        ),
    )

    assert session.committed is False
    assert session.closed is True


@pytest.mark.parametrize(
    ("phase", "status", "revision"),
    [
        ("researching", "running", "revision-1"),
        ("awaiting_outline_approval", "running", "revision-1"),
        ("awaiting_outline_approval", "paused", "revision-stale"),
    ],
)
def test_approve_outline_rejects_state_or_revision_conflicts(
    phase, status, revision
):
    row = FakeCheckpoint()
    row.phase = phase
    row.status = status
    service, session = _service_with_row(row)

    _assert_exception_name(
        "OutlineApprovalConflict",
        lambda: service.approve_outline(
            "session-1", USER_ID, revision, _sections(), _questions()
        ),
    )

    assert session.committed is False
    assert session.rolled_back is True


@pytest.mark.parametrize(
    ("sections", "questions"),
    [
        (_sections(2), _questions()),
        (_sections(13), _questions()),
        (_sections(), _questions(2)),
        (_sections(), _questions(13)),
    ],
)
def test_approve_outline_enforces_plan_count_limits(sections, questions):
    service, session = _service_with_row(FakeCheckpoint())

    _assert_exception_name(
        "ValueError",
        lambda: service.approve_outline(
            "session-1", USER_ID, "revision-1", sections, questions
        ),
    )

    assert session.committed is False
    assert session.rolled_back is True


def test_approve_outline_rejects_duplicate_ids_and_blank_text():
    duplicate_sections = _sections()
    duplicate_sections[1]["id"] = duplicate_sections[0]["id"]
    blank_questions = _questions()
    blank_questions[0]["text"] = "   "

    for sections, questions in (
        (duplicate_sections, _questions()),
        (_sections(), blank_questions),
    ):
        service, session = _service_with_row(FakeCheckpoint())
        _assert_exception_name(
            "ValueError",
            lambda: service.approve_outline(
                "session-1", USER_ID, "revision-1", sections, questions
            ),
        )
        assert session.rolled_back is True


def test_research_checkpoint_declares_unique_session_constraint():
    constraint_names = {
        constraint.name for constraint in ResearchCheckpoint.__table__.constraints
    }

    assert "uq_research_checkpoints_session_id" in constraint_names


def test_save_checkpoint_persists_explicit_status():
    row = FakeCheckpoint()
    service, session = _service_with_row(row)

    checkpoint_id = service.save_checkpoint(
        session_id="session-1",
        state={"query": "产业研究", "phase": "awaiting_outline_approval"},
        user_id=USER_ID,
        status="paused",
    )

    assert checkpoint_id is not None
    assert row.status == "paused"
    assert session.committed is True


def test_clean_state_drops_runtime_objects_and_produces_json():
    created_at = datetime(2026, 7, 19, 8, 30, tzinfo=timezone.utc)
    state = {
        "query": "产业研究",
        "_message_queue": asyncio.Queue(),
        "nested": {
            "owner": UUID(USER_ID),
            "created_at": created_at,
            "items": ["kept", asyncio.Queue()],
        },
    }

    cleaned = CheckpointService()._clean_state_for_storage(state)

    assert "_message_queue" not in cleaned
    assert cleaned["nested"]["owner"] == USER_ID
    assert cleaned["nested"]["created_at"] == created_at.isoformat()
    assert cleaned["nested"]["items"] == ["kept"]
    json.dumps(cleaned)
