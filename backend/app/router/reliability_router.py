"""Authenticated dead-letter inspection and lifecycle endpoints."""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status

from models.user import User
from router.auth_router import get_current_user_required
from service.resilience.persistence import DeadLetterRepository


router = APIRouter(prefix="/reliability", tags=["reliability"])


def get_dead_letter_repository() -> DeadLetterRepository:
    return DeadLetterRepository()


@router.get("/dead-letters")
async def list_dead_letters(
    task_status: Literal["pending", "replaying", "resolved", "discarded"] | None = Query(None, alias="status"),
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    current_user: User = Depends(get_current_user_required),
    repository: DeadLetterRepository = Depends(get_dead_letter_repository),
):
    return {
        "items": repository.list_for_user(
            str(current_user.id), status=task_status, limit=limit, offset=offset
        )
    }


@router.post("/dead-letters/{task_id}/replay")
async def replay_dead_letter(
    task_id: str,
    current_user: User = Depends(get_current_user_required),
    repository: DeadLetterRepository = Depends(get_dead_letter_repository),
):
    item = repository.requeue(task_id, str(current_user.id))
    if item is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="dead-letter task not found")
    return item


@router.post("/dead-letters/{task_id}/resolve")
async def resolve_dead_letter(
    task_id: str,
    current_user: User = Depends(get_current_user_required),
    repository: DeadLetterRepository = Depends(get_dead_letter_repository),
):
    item = repository.resolve(task_id, str(current_user.id))
    if item is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="dead-letter task not found")
    return item
