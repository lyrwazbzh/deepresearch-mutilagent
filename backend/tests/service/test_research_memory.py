from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

from models.research_memory import ExperienceMemory, FactMemory
from service.research_memory.lifecycle import MemoryTieringPolicy
from service.research_memory.repository import canonical_fact_key, content_hash
from service.research_memory.service import ResearchMemoryService
from service.research_memory.types import MemoryQuery


class FakeRepository:
    def __init__(self, rows):
        self.rows = rows
        self.access_calls = []
        self.tier_changes = []

    def get_many(
        self, user_id, ids, *, include_archived=False, include_cold=True
    ):
        ids = set(ids)
        return [
            row for row in self.rows
            if str(row.id) in ids
            and (include_cold or row.storage_tier == "hot")
        ]

    def recent(
        self, user_id, kinds, limit, *, include_archived=False,
        include_cold=False,
    ):
        return [
            row for row in self.rows
            if include_cold or row.storage_tier == "hot"
        ][:limit]

    def record_access(
        self, user_id, ids, *, rehydrate_ids=(), accessed_at=None
    ):
        ids = list(ids)
        rehydrate_ids = set(rehydrate_ids)
        self.access_calls.append((ids, rehydrate_ids))
        for row in self.rows:
            if str(row.id) in ids:
                row.access_count = int(row.access_count or 0) + 1
                row.last_accessed_at = accessed_at or datetime.now(timezone.utc)
            if str(row.id) in rehydrate_ids:
                row.storage_tier = "hot"
        return {"touched": len(ids), "rehydrated": len(rehydrate_ids)}

    def tiering_candidates(self, *, user_id=None, limit=1000):
        return [row for row in self.rows if row.storage_tier == "hot"][:limit]

    def set_storage_tier_many(self, ids, tier, *, changed_at=None):
        ids = set(ids)
        self.tier_changes.append((ids, tier))
        for row in self.rows:
            if str(row.id) in ids:
                row.storage_tier = tier
                row.cold_at = changed_at if tier == "cold" else None
        return len(ids)


class FakeIndex:
    def __init__(self, scores=None):
        self.scores = scores or {}
        self.tier_updates = []

    async def search(self, **kwargs):
        return self.scores

    async def update_tier(self, memory_id, tier):
        self.tier_updates.append((memory_id, tier))


def test_fact_keys_are_stable_and_content_sensitive():
    assert canonical_fact_key(" CATL ", "Market Share") == canonical_fact_key(
        "catl", "market   share"
    )
    assert content_hash("31 %") != content_hash("32 %")


@pytest.mark.asyncio
async def test_planning_context_contains_fact_and_experience():
    now = datetime.now(timezone.utc)
    fact = FactMemory(
        id=uuid4(), user_id=uuid4(), canonical_key="k", content_hash="h",
        subject="储能", predicate="增速", value="20%", content="储能增速为20%",
        confidence=0.9, version=1, status="active", metadata_json={},
        storage_tier="hot", access_count=0,
        created_at=now, updated_at=now,
    )
    experience = ExperienceMemory(
        id=uuid4(), user_id=fact.user_id, run_id="r1", scenario="储能研究",
        trigger_pattern="证据不足", strategy="补充官方来源", outcome="通过评审",
        final_score=8.0, status="active", storage_tier="hot", access_count=0,
        metadata_json={}, created_at=now, updated_at=now,
    )
    service = ResearchMemoryService(repository=FakeRepository([fact, experience]), index=None)
    hits = await service.retrieve(MemoryQuery(
        user_id=str(fact.user_id), query="储能", top_k=5
    ))
    context = service.build_planning_context(hits)
    assert "储能增速为20%" in context
    assert "补充官方来源" in context
    assert len(service.repository.access_calls) == 1
    assert set(service.repository.access_calls[0][0]) == {
        str(fact.id), str(experience.id)
    }


@pytest.mark.asyncio
async def test_relevant_cold_hit_is_rehydrated_and_counted():
    now = datetime.now(timezone.utc)
    fact = FactMemory(
        id=uuid4(), user_id=uuid4(), canonical_key="k", content_hash="h",
        subject="储能", predicate="增速", value="20%", content="储能增速为20%",
        confidence=0.95, version=1, status="active", storage_tier="cold",
        access_count=2, metadata_json={}, created_at=now - timedelta(days=200),
        updated_at=now - timedelta(days=200), cold_at=now - timedelta(days=10),
    )
    index = FakeIndex({str(fact.id): {"vector": 1.0, "bm25": 1.0}})
    repository = FakeRepository([fact])
    service = ResearchMemoryService(repository=repository, index=index)

    hits = await service.retrieve(MemoryQuery(
        user_id=str(fact.user_id), query="储能增速", top_k=5
    ))

    assert len(hits) == 1
    assert hits[0].rehydrated is True
    assert hits[0].storage_tier == "hot"
    assert fact.storage_tier == "hot"
    assert repository.access_calls[0][1] == {str(fact.id)}
    assert index.tier_updates == [(str(fact.id), "hot")]


def test_heat_policy_uses_recency_frequency_and_confidence():
    now = datetime.now(timezone.utc)
    policy = MemoryTieringPolicy(
        minimum_hot_days=30,
        cold_threshold=0.35,
        recency_half_life_days=30,
        frequency_saturation=8,
    )
    cold_candidate = FactMemory(
        id=uuid4(), user_id=uuid4(), canonical_key="a", content_hash="a",
        subject="a", predicate="a", value="a", content="a",
        confidence=0.2, version=1, status="active", storage_tier="hot",
        access_count=0, created_at=now - timedelta(days=180),
        updated_at=now - timedelta(days=180), metadata_json={},
    )
    frequently_used = FactMemory(
        id=uuid4(), user_id=cold_candidate.user_id, canonical_key="b", content_hash="b",
        subject="b", predicate="b", value="b", content="b",
        confidence=0.9, version=1, status="active", storage_tier="hot",
        access_count=8, last_accessed_at=now - timedelta(days=5),
        created_at=now - timedelta(days=180), updated_at=now - timedelta(days=180),
        metadata_json={},
    )

    assert policy.should_move_to_cold(cold_candidate, now=now) is True
    assert policy.should_move_to_cold(frequently_used, now=now) is False


@pytest.mark.asyncio
async def test_tiering_cycle_moves_only_low_heat_rows():
    now = datetime.now(timezone.utc)
    old = FactMemory(
        id=uuid4(), user_id=uuid4(), canonical_key="old", content_hash="old",
        subject="old", predicate="old", value="old", content="old",
        confidence=0.1, version=1, status="active", storage_tier="hot",
        access_count=0, created_at=now - timedelta(days=365),
        updated_at=now - timedelta(days=365), metadata_json={},
    )
    fresh = FactMemory(
        id=uuid4(), user_id=old.user_id, canonical_key="fresh", content_hash="fresh",
        subject="fresh", predicate="fresh", value="fresh", content="fresh",
        confidence=0.8, version=1, status="active", storage_tier="hot",
        access_count=0, created_at=now, updated_at=now, metadata_json={},
    )
    repository = FakeRepository([old, fresh])
    index = FakeIndex()
    service = ResearchMemoryService(
        repository=repository,
        index=index,
        tiering_policy=MemoryTieringPolicy(
            minimum_hot_days=30,
            cold_threshold=0.35,
            recency_half_life_days=30,
        ),
    )

    result = await service.run_tiering_cycle(now=now)

    assert result == {"scanned": 2, "moved_to_cold": 1}
    assert old.storage_tier == "cold"
    assert fresh.storage_tier == "hot"
    assert index.tier_updates == [(str(old.id), "cold")]


def test_experience_is_induced_from_real_critic_actions():
    state = {
        "query": "储能市场研究",
        "quality_score": 8.2,
        "quality_gate_passed": True,
        "quality_gate_reasons": [],
        "critic_feedback": [{
            "issue_type": "missing_source",
            "description": "缺少装机量原始来源",
            "suggestion": "补充能源局数据",
        }],
        "review_history": [
            {
                "round": 1,
                "quality_score": 5.5,
                "issue_types": ["missing_source"],
                "suggestions": ["补充能源局数据"],
                "search_queries": ["国家能源局 储能装机量"],
                "next_action": "re_research",
            },
            {
                "round": 2,
                "quality_score": 8.2,
                "issue_types": [],
                "suggestions": [],
                "search_queries": [],
                "next_action": "complete",
            },
        ],
    }

    experience = ResearchMemoryService._induce_experience(
        state=state, run_id="run-1"
    )

    assert experience is not None
    assert experience.initial_score == 5.5
    assert experience.final_score == 8.2
    assert "missing_source×1" in experience.trigger
    assert "国家能源局 储能装机量" in experience.strategy
    assert "5.5→8.2" in experience.outcome
    assert experience.metadata["actions"] == ["re_research", "complete"]
