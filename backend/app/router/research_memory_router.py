"""Authenticated Fact/Experience memory lifecycle endpoints."""

from __future__ import annotations

import asyncio
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status

from models.research_memory import ExperienceMemory, FactMemory
from models.user import User
from router.auth_router import get_current_user_required
from service.research_memory.factory import get_research_memory_service
from service.research_memory.types import MemoryQuery


router = APIRouter(prefix="/research-memories", tags=["research-memory"])


def _serialize(row):
    common = {
        "id": str(row.id),
        "kind": "fact" if isinstance(row, FactMemory) else "experience",
        "status": row.status,
        "storage_tier": getattr(row, "storage_tier", "hot"),
        "last_accessed_at": getattr(row, "last_accessed_at", None),
        "access_count": int(getattr(row, "access_count", 0) or 0),
        "cold_at": getattr(row, "cold_at", None),
        "rehydrated_at": getattr(row, "rehydrated_at", None),
        "created_at": row.created_at,
        "updated_at": row.updated_at,
        "metadata": row.metadata_json or {},
    }
    if isinstance(row, FactMemory):
        return {
            **common,
            "subject": row.subject,
            "predicate": row.predicate,
            "value": row.value,
            "content": row.content,
            "confidence": row.confidence,
            "version": row.version,
            "parent_id": str(row.parent_id) if row.parent_id else None,
            "source_url": row.source_url,
        }
    return {
        **common,
        "run_id": row.run_id,
        "scenario": row.scenario,
        "trigger": row.trigger_pattern,
        "strategy": row.strategy,
        "outcome": row.outcome,
        "critic_summary": row.critic_summary,
        "initial_score": row.initial_score,
        "final_score": row.final_score,
    }


@router.get("")
async def list_memories(
    kinds: list[Literal["fact", "experience"]] = Query(default=["fact", "experience"]),
    limit: int = Query(20, ge=1, le=100),
    include_archived: bool = False,
    include_cold: bool = False,
    current_user: User = Depends(get_current_user_required),
    service=Depends(get_research_memory_service),
):
    rows = await asyncio.to_thread(
        service.repository.recent,
        str(current_user.id),
        kinds,
        limit,
        include_archived=include_archived,
        include_cold=include_cold,
    )
    return {"items": [_serialize(row) for row in rows]}


@router.get("/search")
async def search_memories(
    query: str = Query(min_length=1),
    kinds: list[Literal["fact", "experience"]] = Query(default=["fact", "experience"]),
    top_k: int = Query(5, ge=1, le=20),
    min_confidence: float = Query(0.0, ge=0.0, le=1.0),
    include_archived: bool = False,
    current_user: User = Depends(get_current_user_required),
    service=Depends(get_research_memory_service),
):
    hits = await service.retrieve(MemoryQuery(
        user_id=str(current_user.id),
        query=query,
        kinds=kinds,
        top_k=top_k,
        min_confidence=min_confidence,
        include_archived=include_archived,
    ))
    return {"items": [hit.model_dump(mode="json") for hit in hits]}


@router.get("/{memory_id}")
async def get_memory(
    memory_id: str,
    current_user: User = Depends(get_current_user_required),
    service=Depends(get_research_memory_service),
):
    row = await service.get_and_touch(str(current_user.id), memory_id)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="memory not found")
    return _serialize(row)


@router.get("/{memory_id}/versions")
async def list_fact_versions(
    memory_id: str,
    current_user: User = Depends(get_current_user_required),
    service=Depends(get_research_memory_service),
):
    rows = await asyncio.to_thread(
        service.repository.fact_versions, str(current_user.id), memory_id
    )
    return {"items": [_serialize(row) for row in rows]}


@router.post("/{memory_id}/archive")
async def archive_memory(
    memory_id: str,
    current_user: User = Depends(get_current_user_required),
    service=Depends(get_research_memory_service),
):
    updated = await asyncio.to_thread(
        service.repository.set_status, str(current_user.id), memory_id, "archived"
    )
    if not updated:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="memory not found")
    return {"id": memory_id, "status": "archived"}
