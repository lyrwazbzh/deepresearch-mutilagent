"""PostgreSQL repository with fact versioning and conflict handling."""

from __future__ import annotations

import hashlib
import uuid
from datetime import datetime, timezone
from typing import Iterable
from sqlalchemy import text

from core.database import SessionLocal
from models.research_memory import ExperienceMemory, FactMemory

from .types import ExperienceMemoryInput, FactMemoryInput


def canonical_fact_key(subject: str, predicate: str) -> str:
    normalized = " ".join(f"{subject.strip().lower()}|{predicate.strip().lower()}".split())
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def content_hash(value: str) -> str:
    normalized = " ".join(value.strip().lower().split())
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


class ResearchMemoryRepository:
    def upsert_fact(self, user_id: str, item: FactMemoryInput) -> tuple[FactMemory, bool]:
        key = canonical_fact_key(item.subject, item.predicate)
        digest = content_hash(item.value)
        with SessionLocal() as db:
            db.execute(
                text("SELECT pg_advisory_xact_lock(hashtext(:lock_key))"),
                {"lock_key": f"fact:{user_id}:{key}"},
            )
            versions = db.query(FactMemory).filter(
                FactMemory.user_id == uuid.UUID(user_id),
                FactMemory.canonical_key == key,
            ).order_by(FactMemory.version.desc()).all()
            for existing in versions:
                if existing.content_hash == digest:
                    existing.confidence = max(existing.confidence, item.confidence)
                    existing.updated_at = datetime.now(timezone.utc)
                    db.commit()
                    db.refresh(existing)
                    db.expunge(existing)
                    return existing, False

            previous = versions[0] if versions else None
            version = (previous.version + 1) if previous else 1
            row = FactMemory(
                user_id=uuid.UUID(user_id),
                canonical_key=key,
                content_hash=digest,
                subject=item.subject,
                predicate=item.predicate,
                value=item.value,
                content=item.content,
                source_url=item.source_url,
                source_name=item.source_name,
                source_type=item.source_type,
                confidence=item.confidence,
                observed_at=item.observed_at,
                version=version,
                parent_id=previous.id if previous else None,
                metadata_json={**item.metadata, "run_id": item.run_id},
                status=(
                    "active"
                    if previous is None or item.confidence >= previous.confidence
                    else "archived"
                ),
            )
            if previous and item.confidence >= previous.confidence:
                previous.status = "invalidated"
            db.add(row)
            db.commit()
            db.refresh(row)
            db.expunge(row)
            return row, True

    def append_experience(self, user_id: str, item: ExperienceMemoryInput) -> ExperienceMemory:
        with SessionLocal() as db:
            row = ExperienceMemory(
                user_id=uuid.UUID(user_id),
                run_id=item.run_id,
                scenario=item.scenario,
                trigger_pattern=item.trigger,
                strategy=item.strategy,
                outcome=item.outcome,
                critic_summary=item.critic_summary,
                initial_score=item.initial_score,
                final_score=item.final_score,
                metadata_json=item.metadata,
            )
            db.add(row)
            db.commit()
            db.refresh(row)
            db.expunge(row)
            return row

    def get_many(
        self,
        user_id: str,
        ids: Iterable[str],
        *,
        include_archived: bool = False,
        include_cold: bool = True,
    ):
        parsed = [uuid.UUID(item) for item in ids]
        if not parsed:
            return []
        with SessionLocal() as db:
            facts_query = db.query(FactMemory).filter(
                FactMemory.user_id == uuid.UUID(user_id), FactMemory.id.in_(parsed)
            )
            experiences_query = db.query(ExperienceMemory).filter(
                ExperienceMemory.user_id == uuid.UUID(user_id), ExperienceMemory.id.in_(parsed)
            )
            if not include_archived:
                facts_query = facts_query.filter(FactMemory.status == "active")
                experiences_query = experiences_query.filter(ExperienceMemory.status == "active")
            if not include_cold:
                facts_query = facts_query.filter(FactMemory.storage_tier == "hot")
                experiences_query = experiences_query.filter(
                    ExperienceMemory.storage_tier == "hot"
                )
            facts = facts_query.all()
            experiences = experiences_query.all()
            for row in [*facts, *experiences]:
                db.expunge(row)
            return [*facts, *experiences]

    def recent(
        self,
        user_id: str,
        kinds: list[str],
        limit: int,
        *,
        include_archived: bool = False,
        include_cold: bool = False,
    ):
        rows = []
        with SessionLocal() as db:
            if "fact" in kinds:
                query = db.query(FactMemory).filter(FactMemory.user_id == uuid.UUID(user_id))
                if not include_archived:
                    query = query.filter(FactMemory.status == "active")
                if not include_cold:
                    query = query.filter(FactMemory.storage_tier == "hot")
                rows.extend(query.order_by(FactMemory.updated_at.desc()).limit(limit).all())
            if "experience" in kinds:
                query = db.query(ExperienceMemory).filter(
                    ExperienceMemory.user_id == uuid.UUID(user_id)
                )
                if not include_archived:
                    query = query.filter(ExperienceMemory.status == "active")
                if not include_cold:
                    query = query.filter(ExperienceMemory.storage_tier == "hot")
                rows.extend(query.order_by(ExperienceMemory.updated_at.desc()).limit(limit).all())
            for row in rows:
                db.expunge(row)
        rows.sort(key=lambda row: row.updated_at, reverse=True)
        return rows[:limit]

    def get_one(self, user_id: str, memory_id: str):
        with SessionLocal() as db:
            parsed_user = uuid.UUID(user_id)
            parsed_id = uuid.UUID(memory_id)
            row = db.query(FactMemory).filter(
                FactMemory.id == parsed_id, FactMemory.user_id == parsed_user
            ).first() or db.query(ExperienceMemory).filter(
                ExperienceMemory.id == parsed_id, ExperienceMemory.user_id == parsed_user
            ).first()
            if row:
                db.expunge(row)
            return row

    def record_access(
        self,
        user_id: str,
        memory_ids: Iterable[str],
        *,
        rehydrate_ids: Iterable[str] = (),
        accessed_at: datetime | None = None,
    ) -> dict[str, int]:
        """Atomically count returned hits and promote eligible cold rows."""

        parsed_ids = [uuid.UUID(item) for item in dict.fromkeys(memory_ids)]
        if not parsed_ids:
            return {"touched": 0, "rehydrated": 0}
        rehydrate = {
            uuid.UUID(item) for item in dict.fromkeys(rehydrate_ids)
        }
        now = accessed_at or datetime.now(timezone.utc)
        touched = 0
        promoted = 0
        with SessionLocal() as db:
            parsed_user = uuid.UUID(user_id)
            rows = [
                *db.query(FactMemory).filter(
                    FactMemory.user_id == parsed_user,
                    FactMemory.id.in_(parsed_ids),
                ).with_for_update().all(),
                *db.query(ExperienceMemory).filter(
                    ExperienceMemory.user_id == parsed_user,
                    ExperienceMemory.id.in_(parsed_ids),
                ).with_for_update().all(),
            ]
            for row in rows:
                row.last_accessed_at = now
                row.access_count = int(row.access_count or 0) + 1
                touched += 1
                if row.id in rehydrate and row.status == "active":
                    if row.storage_tier == "cold":
                        promoted += 1
                    row.storage_tier = "hot"
                    row.rehydrated_at = now
                    row.cold_at = None
            db.commit()
        return {"touched": touched, "rehydrated": promoted}

    def tiering_candidates(
        self,
        *,
        user_id: str | None = None,
        limit: int = 1000,
    ):
        """Return active hot rows for one bounded lifecycle sweep."""

        rows = []
        with SessionLocal() as db:
            for model in (FactMemory, ExperienceMemory):
                query = db.query(model).filter(
                    model.status == "active",
                    model.storage_tier == "hot",
                )
                if user_id:
                    query = query.filter(model.user_id == uuid.UUID(user_id))
                batch = query.order_by(model.updated_at.asc()).limit(limit).all()
                for row in batch:
                    db.expunge(row)
                rows.extend(batch)
        return rows

    def set_storage_tier_many(
        self,
        memory_ids: Iterable[str],
        tier: str,
        *,
        changed_at: datetime | None = None,
    ) -> int:
        if tier not in {"hot", "cold"}:
            raise ValueError("tier must be hot or cold")
        parsed_ids = [uuid.UUID(item) for item in dict.fromkeys(memory_ids)]
        if not parsed_ids:
            return 0
        now = changed_at or datetime.now(timezone.utc)
        updated = 0
        with SessionLocal() as db:
            for model in (FactMemory, ExperienceMemory):
                rows = db.query(model).filter(model.id.in_(parsed_ids)).with_for_update().all()
                for row in rows:
                    row.storage_tier = tier
                    if tier == "cold":
                        row.cold_at = now
                    else:
                        row.cold_at = None
                        row.rehydrated_at = now
                    updated += 1
            db.commit()
        return updated

    def fact_versions(self, user_id: str, memory_id: str):
        with SessionLocal() as db:
            parsed_user = uuid.UUID(user_id)
            parsed_id = uuid.UUID(memory_id)
            seed = db.query(FactMemory).filter(
                FactMemory.id == parsed_id, FactMemory.user_id == parsed_user
            ).first()
            if not seed:
                return []
            rows = db.query(FactMemory).filter(
                FactMemory.user_id == parsed_user,
                FactMemory.canonical_key == seed.canonical_key,
            ).order_by(FactMemory.version.desc()).all()
            for row in rows:
                db.expunge(row)
            return rows

    def set_status(self, user_id: str, memory_id: str, status: str) -> bool:
        with SessionLocal() as db:
            parsed_user = uuid.UUID(user_id)
            parsed_id = uuid.UUID(memory_id)
            row = db.query(FactMemory).filter(
                FactMemory.id == parsed_id, FactMemory.user_id == parsed_user
            ).first() or db.query(ExperienceMemory).filter(
                ExperienceMemory.id == parsed_id, ExperienceMemory.user_id == parsed_user
            ).first()
            if not row:
                return False
            row.status = status
            db.commit()
            return True
