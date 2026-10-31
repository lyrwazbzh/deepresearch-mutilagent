"""Transactional run and event ledger for deep-research diagnostics."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Callable
from uuid import UUID, uuid4

from sqlalchemy import func
from sqlalchemy.orm import Session

from core.database import SessionLocal
from models.chat import ChatSession
from models.observability import ResearchEvent, ResearchRun
from observability.events import sanitize_event_payload


logger = logging.getLogger(__name__)
SessionFactory = Callable[[], Session]
_TERMINAL_STATUSES = {"completed", "failed", "cancelled", "paused"}


class ResearchObservabilityService:
    """Persist a compact, user-scoped history of each research attempt."""

    def __init__(self, session_factory: SessionFactory = SessionLocal) -> None:
        self._session_factory = session_factory

    def start_run(
        self,
        *,
        session_id: str,
        user_id: str,
        query: str,
        research_id: str | None = None,
        request_id: str | None = None,
        trace_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, object]:
        with self._session_factory() as db:
            run = ResearchRun(
                id=uuid4(),
                research_id=UUID(research_id) if research_id else uuid4(),
                session_id=UUID(session_id),
                user_id=UUID(user_id),
                request_id=request_id,
                trace_id=trace_id,
                query_summary=sanitize_event_payload({"query": query})["query"],
                metadata_json=sanitize_event_payload(metadata or {}),
                status="running",
            )
            db.add(run)
            db.commit()
            db.refresh(run)
            return run.to_dict()

    def record_event(
        self,
        *,
        run_id: str,
        event_type: str,
        phase: str | None = None,
        status: str = "info",
        payload: dict[str, Any] | None = None,
        duration_ms: int | None = None,
        trace_id: str | None = None,
        span_id: str | None = None,
        fail_silently: bool = True,
    ) -> dict[str, object] | None:
        try:
            with self._session_factory() as db:
                run = (
                    db.query(ResearchRun)
                    .filter(ResearchRun.id == UUID(run_id))
                    .with_for_update()
                    .one_or_none()
                )
                if run is None:
                    return None
                latest_sequence = (
                    db.query(func.max(ResearchEvent.sequence))
                    .filter(ResearchEvent.run_id == run.id)
                    .scalar()
                    or 0
                )
                event = ResearchEvent(
                    run_id=run.id,
                    sequence=latest_sequence + 1,
                    event_type=event_type,
                    phase=phase,
                    status=status,
                    payload=sanitize_event_payload(payload or {}),
                    duration_ms=duration_ms,
                    trace_id=trace_id or run.trace_id,
                    span_id=span_id,
                )
                if phase:
                    run.current_phase = phase
                db.add(event)
                db.commit()
                db.refresh(event)
                return event.to_dict()
        except Exception:
            logger.exception(
                "Failed to persist research event",
                extra={"event": "observability.event.persistence_failed", "details": {"event_type": event_type}},
            )
            if fail_silently:
                return None
            raise

    def finish_run(
        self,
        *,
        run_id: str,
        status: str,
        input_tokens: int = 0,
        output_tokens: int = 0,
        estimated_cost: float | None = None,
        error_code: str | None = None,
        error_summary: str | None = None,
    ) -> dict[str, object] | None:
        if status not in _TERMINAL_STATUSES:
            raise ValueError(f"Invalid terminal research status: {status}")
        with self._session_factory() as db:
            run = db.query(ResearchRun).filter(ResearchRun.id == UUID(run_id)).with_for_update().one_or_none()
            if run is None:
                return None
            ended_at = datetime.now(UTC)
            started_at = run.started_at
            if started_at.tzinfo is None:
                started_at = started_at.replace(tzinfo=UTC)
            run.status = status
            run.ended_at = ended_at
            run.duration_ms = max(0, round((ended_at - started_at).total_seconds() * 1000))
            run.input_tokens = max(0, input_tokens)
            run.output_tokens = max(0, output_tokens)
            run.estimated_cost = Decimal(str(estimated_cost)) if estimated_cost is not None else None
            run.error_code = error_code
            run.error_summary = error_summary[:1000] if error_summary else None
            db.commit()
            db.refresh(run)
            return run.to_dict()

    def get_run(self, run_id: str, *, user_id: str) -> dict[str, object] | None:
        with self._session_factory() as db:
            run = (
                db.query(ResearchRun)
                .filter(ResearchRun.id == UUID(run_id), ResearchRun.user_id == UUID(user_id))
                .one_or_none()
            )
            return run.to_dict() if run else None

    def list_runs(
        self,
        *,
        session_id: str,
        user_id: str,
        limit: int = 50,
    ) -> dict[str, object]:
        bounded_limit = min(max(limit, 1), 100)
        with self._session_factory() as db:
            runs = (
                db.query(ResearchRun)
                .filter(
                    ResearchRun.session_id == UUID(session_id),
                    ResearchRun.user_id == UUID(user_id),
                )
                .order_by(ResearchRun.started_at.desc())
                .limit(bounded_limit)
                .all()
            )
            return {"items": [run.to_dict() for run in runs], "next_cursor": None}

    def list_events(
        self,
        *,
        run_id: str,
        user_id: str,
        after_sequence: int = 0,
        limit: int = 100,
    ) -> dict[str, object]:
        bounded_limit = min(max(limit, 1), 200)
        with self._session_factory() as db:
            events = (
                db.query(ResearchEvent)
                .join(ResearchRun, ResearchRun.id == ResearchEvent.run_id)
                .filter(
                    ResearchEvent.run_id == UUID(run_id),
                    ResearchRun.user_id == UUID(user_id),
                    ResearchEvent.sequence > max(after_sequence, 0),
                )
                .order_by(ResearchEvent.sequence.asc())
                .limit(bounded_limit + 1)
                .all()
            )
            has_more = len(events) > bounded_limit
            page = events[:bounded_limit]
            return {
                "items": [event.to_dict() for event in page],
                "next_cursor": page[-1].sequence if has_more and page else None,
            }

    def get_timeline(
        self,
        *,
        session_id: str,
        user_id: str,
        run_limit: int = 50,
        event_limit: int = 500,
    ) -> dict[str, object] | None:
        """Return a bounded chronological view after verifying session ownership."""

        session_uuid = UUID(session_id)
        user_uuid = UUID(user_id)
        with self._session_factory() as db:
            owns_session = (
                db.query(ChatSession.id)
                .filter(ChatSession.id == session_uuid, ChatSession.user_id == user_uuid)
                .one_or_none()
            )
            if owns_session is None:
                return None

            runs = (
                db.query(ResearchRun)
                .filter(ResearchRun.session_id == session_uuid, ResearchRun.user_id == user_uuid)
                .order_by(ResearchRun.started_at.desc())
                .limit(min(max(run_limit, 1), 100))
                .all()
            )
            events = (
                db.query(ResearchEvent)
                .join(ResearchRun, ResearchRun.id == ResearchEvent.run_id)
                .filter(ResearchRun.session_id == session_uuid, ResearchRun.user_id == user_uuid)
                .order_by(ResearchEvent.created_at.asc(), ResearchEvent.sequence.asc())
                .limit(min(max(event_limit, 1), 1000))
                .all()
            )
            return {
                "runs": [run.to_dict() for run in runs],
                "events": [event.to_dict() for event in events],
                "next_cursor": None,
            }
