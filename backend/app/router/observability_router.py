"""Authenticated diagnostics APIs for deep-research runs and events."""

from __future__ import annotations

from functools import lru_cache
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status

from models.user import User
from router.auth_router import get_current_user_required
from service.research_observability_service import ResearchObservabilityService


router = APIRouter(prefix="/research", tags=["research diagnostics"])


@lru_cache(maxsize=1)
def get_observability_service() -> ResearchObservabilityService:
    return ResearchObservabilityService()


def _validated_uuid(value: str, *, field_name: str) -> str:
    try:
        return str(UUID(value))
    except (TypeError, ValueError, AttributeError) as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid {field_name}",
        ) from exc


@router.get("/runs")
async def list_research_runs(
    session_id: str = Query(...),
    limit: int = Query(50, ge=1, le=100),
    current_user: User = Depends(get_current_user_required),
    service: ResearchObservabilityService = Depends(get_observability_service),
):
    return service.list_runs(
        session_id=_validated_uuid(session_id, field_name="session_id"),
        user_id=str(current_user.id),
        limit=limit,
    )


@router.get("/runs/{run_id}")
async def get_research_run(
    run_id: str,
    current_user: User = Depends(get_current_user_required),
    service: ResearchObservabilityService = Depends(get_observability_service),
):
    run = service.get_run(
        _validated_uuid(run_id, field_name="run_id"),
        user_id=str(current_user.id),
    )
    if run is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Research run not found")
    return run


@router.get("/runs/{run_id}/events")
async def list_research_events(
    run_id: str,
    after_sequence: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=200),
    current_user: User = Depends(get_current_user_required),
    service: ResearchObservabilityService = Depends(get_observability_service),
):
    validated_run_id = _validated_uuid(run_id, field_name="run_id")
    user_id = str(current_user.id)
    if service.get_run(validated_run_id, user_id=user_id) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Research run not found")
    return service.list_events(
        run_id=validated_run_id,
        user_id=user_id,
        after_sequence=after_sequence,
        limit=limit,
    )


@router.get("/sessions/{session_id}/timeline")
async def get_research_timeline(
    session_id: str,
    current_user: User = Depends(get_current_user_required),
    service: ResearchObservabilityService = Depends(get_observability_service),
):
    timeline = service.get_timeline(
        session_id=_validated_uuid(session_id, field_name="session_id"),
        user_id=str(current_user.id),
    )
    if timeline is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Session not found")
    return timeline

