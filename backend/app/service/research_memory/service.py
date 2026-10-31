"""Fact/Experience memory lifecycle and planning-context construction."""

from __future__ import annotations

import asyncio
import math
from collections import Counter
from datetime import datetime, timezone
from typing import Any

from models.research_memory import ExperienceMemory, FactMemory
from observability.metrics import application_metrics

from .lifecycle import MemoryTieringPolicy
from .repository import ResearchMemoryRepository
from .types import ExperienceMemoryInput, FactMemoryInput, MemoryHit, MemoryQuery


class ResearchMemoryService:
    def __init__(self, *, repository=None, index=None, tiering_policy=None):
        self.repository = repository or ResearchMemoryRepository()
        self.index = index
        self.tiering_policy = tiering_policy or MemoryTieringPolicy.from_environment()

    async def store_verified_facts(self, user_id: str, facts: list[FactMemoryInput]):
        written = 0
        merged = 0
        for item in facts:
            row, created = await asyncio.to_thread(self.repository.upsert_fact, user_id, item)
            written += int(created)
            merged += int(not created)
            if created and row.status == "active" and self.index is not None:
                await self.index.index(
                    memory_id=str(row.id), user_id=user_id, kind="fact",
                    content=row.content, confidence=row.confidence,
                    metadata={"version": row.version, "source_url": row.source_url},
                    storage_tier=getattr(row, "storage_tier", "hot"),
                )
        application_metrics.memory_operations.labels("fact_store", "success").inc(written + merged)
        return {"written": written, "merged": merged}

    async def store_experience(self, user_id: str, item: ExperienceMemoryInput):
        row = await asyncio.to_thread(self.repository.append_experience, user_id, item)
        if self.index is not None:
            await self.index.index(
                memory_id=str(row.id), user_id=user_id, kind="experience",
                content=f"{row.scenario}\n{row.strategy}\n{row.outcome}",
                confidence=self.tiering_policy.confidence(row),
                metadata={"run_id": row.run_id},
                storage_tier=getattr(row, "storage_tier", "hot"),
            )
        application_metrics.memory_operations.labels("experience_store", "success").inc()
        return str(row.id)

    async def retrieve(self, query: MemoryQuery) -> list[MemoryHit]:
        scores = {}
        if self.index is not None and not query.include_archived:
            try:
                scores = await self.index.search(
                    user_id=query.user_id, query=query.query,
                    kinds=query.kinds, top_k=query.top_k,
                )
            except Exception:
                scores = {}
        if scores:
            rows = await asyncio.to_thread(
                self.repository.get_many,
                query.user_id,
                scores.keys(),
                include_archived=query.include_archived,
                include_cold=True,
            )
        else:
            rows = await asyncio.to_thread(
                self.repository.recent,
                query.user_id,
                query.kinds,
                query.top_k,
                include_archived=query.include_archived,
                include_cold=False,
            )
        now = datetime.now(timezone.utc)
        hits = []
        rows_by_id = {str(row.id): row for row in rows}
        for row in rows:
            kind = "fact" if isinstance(row, FactMemory) else "experience"
            confidence = self.tiering_policy.confidence(row)
            if confidence < query.min_confidence:
                continue
            created_at = row.created_at
            if created_at.tzinfo is None:
                created_at = created_at.replace(tzinfo=timezone.utc)
            age_days = max((now - created_at).total_seconds() / 86400, 0)
            time_score = math.exp(-age_days / 180)
            component = scores.get(str(row.id), {})
            vector_score = component.get("vector", 0.0)
            bm25_score = component.get("bm25", 0.0)
            score = 0.55 * vector_score + 0.30 * bm25_score + 0.10 * time_score + 0.05 * confidence
            storage_tier = getattr(row, "storage_tier", "hot") or "hot"
            rehydrated = False
            if storage_tier == "cold":
                if not query.allow_cold_rehydrate or not self.tiering_policy.should_rehydrate(
                    row, retrieval_score=score
                ):
                    continue
                rehydrated = True
            content = row.content if kind == "fact" else f"{row.scenario}；策略：{row.strategy}；结果：{row.outcome}"
            hits.append(MemoryHit(
                id=str(row.id), kind=kind, content=content, score=score,
                vector_score=vector_score, bm25_score=bm25_score,
                time_score=time_score, confidence=confidence,
                storage_tier="hot" if rehydrated else storage_tier,
                rehydrated=rehydrated,
                metadata={
                    **(row.metadata_json or {}),
                    "heat_score": self.tiering_policy.heat_score(row, now=now),
                    "previous_storage_tier": storage_tier,
                },
            ))
        hits = sorted(hits, key=lambda item: item.score, reverse=True)[:query.top_k]
        active_hit_ids = [
            hit.id for hit in hits
            if getattr(rows_by_id[hit.id], "status", "active") == "active"
        ]
        rehydrate_ids = [hit.id for hit in hits if hit.rehydrated]
        if active_hit_ids:
            access_result = await asyncio.to_thread(
                self.repository.record_access,
                query.user_id,
                active_hit_ids,
                rehydrate_ids=rehydrate_ids,
                accessed_at=now,
            )
            application_metrics.memory_operations.labels("access", "success").inc(
                access_result.get("touched", 0)
            )
        if self.index is not None:
            for memory_id in rehydrate_ids:
                await self.index.update_tier(memory_id, "hot")
        if rehydrate_ids:
            application_metrics.memory_operations.labels("rehydrate", "success").inc(
                len(rehydrate_ids)
            )
        application_metrics.memory_operations.labels("retrieve", "success").inc()
        application_metrics.memory_retrieval_hits.observe(len(hits))
        return hits

    @staticmethod
    def build_planning_context(hits: list[MemoryHit]) -> str:
        if not hits:
            return ""
        facts = [hit.content for hit in hits if hit.kind == "fact"]
        experiences = [hit.content for hit in hits if hit.kind == "experience"]
        blocks = []
        if facts:
            blocks.append("已验证事实：\n" + "\n".join(f"- {item}" for item in facts))
        if experiences:
            blocks.append("历史研究策略：\n" + "\n".join(f"- {item}" for item in experiences))
        return "\n\n".join(blocks)

    async def get_and_touch(self, user_id: str, memory_id: str):
        """Treat an explicit read as access and immediately rehydrate cold data."""

        row = await asyncio.to_thread(self.repository.get_one, user_id, memory_id)
        if row is None:
            return None
        if getattr(row, "status", "active") == "active":
            was_cold = getattr(row, "storage_tier", "hot") == "cold"
            await asyncio.to_thread(
                self.repository.record_access,
                user_id,
                [memory_id],
                rehydrate_ids=[memory_id] if was_cold else [],
            )
            row.access_count = int(getattr(row, "access_count", 0) or 0) + 1
            row.last_accessed_at = datetime.now(timezone.utc)
            if was_cold:
                row.storage_tier = "hot"
                row.cold_at = None
                row.rehydrated_at = row.last_accessed_at
                if self.index is not None:
                    await self.index.update_tier(memory_id, "hot")
                application_metrics.memory_operations.labels(
                    "rehydrate", "success"
                ).inc()
        return row

    async def run_tiering_cycle(
        self,
        *,
        user_id: str | None = None,
        limit: int = 1000,
        now: datetime | None = None,
    ) -> dict[str, int]:
        """Move low-heat active memories to the cold tier."""

        now = now or datetime.now(timezone.utc)
        rows = await asyncio.to_thread(
            self.repository.tiering_candidates,
            user_id=user_id,
            limit=limit,
        )
        cold_ids = [
            str(row.id)
            for row in rows
            if self.tiering_policy.should_move_to_cold(row, now=now)
        ]
        moved = await asyncio.to_thread(
            self.repository.set_storage_tier_many,
            cold_ids,
            "cold",
            changed_at=now,
        )
        if self.index is not None:
            for memory_id in cold_ids:
                await self.index.update_tier(memory_id, "cold")
        if moved:
            application_metrics.memory_operations.labels("cool", "success").inc(moved)
        return {"scanned": len(rows), "moved_to_cold": moved}

    async def capture_completed_run(self, *, user_id: str, state: dict[str, Any], run_id: str):
        fact_inputs = []
        for fact in state.get("facts", []):
            credibility = str(fact.get("credibility", "")).lower()
            confidence = float(fact.get(
                "confidence",
                fact.get("credibility_score", 0.8 if credibility in {"high", "高"} else 0.6),
            ))
            confidence = min(max(confidence, 0.0), 1.0)
            if not fact.get("verified") and confidence < 0.7:
                continue
            content = str(fact.get("content", "")).strip()
            if not content:
                continue
            fact_inputs.append(FactMemoryInput(
                subject=str(fact.get("subject") or state.get("query", "研究主题"))[:500],
                predicate=str(fact.get("predicate") or "相关事实")[:255],
                value=str(fact.get("value") or content),
                content=content,
                source_url=fact.get("source_url"),
                source_name=fact.get("source_name"),
                source_type=fact.get("source_type"),
                confidence=confidence,
                run_id=run_id,
            ))
        result = await self.store_verified_facts(user_id, fact_inputs)
        experience = self._induce_experience(state=state, run_id=run_id)
        if experience is not None:
            result["experience_id"] = await self.store_experience(user_id, experience)
        return result

    @staticmethod
    def _induce_experience(
        *,
        state: dict[str, Any],
        run_id: str,
    ) -> ExperienceMemoryInput | None:
        """Distil Critic rounds into a reusable trigger → strategy → outcome record."""

        history = [
            item for item in state.get("review_history", [])
            if isinstance(item, dict)
        ]
        feedback = [
            item for item in state.get("critic_feedback", [])
            if isinstance(item, dict)
        ]
        if not history and not feedback:
            return None

        issue_counter = Counter(
            str(issue_type)
            for item in history
            for issue_type in item.get("issue_types", [])
        )
        if not issue_counter:
            issue_counter.update(
                str(item.get("issue_type", "unknown")) for item in feedback
            )
        issue_profile = "、".join(
            f"{name}×{count}" for name, count in issue_counter.most_common()
        ) or "无阻断问题"

        first_score = (
            float(history[0].get("quality_score", 0.0))
            if history else None
        )
        final_score = (
            float(history[-1].get("quality_score", state.get("quality_score", 0.0)))
            if history else float(state.get("quality_score", 0.0) or 0.0)
        )
        trigger = (
            f"首轮评分 {first_score:g}；问题画像：{issue_profile}"
            if first_score is not None
            else f"Critic 问题画像：{issue_profile}"
        )

        strategy_steps = []
        for item in history:
            round_number = item.get("round", len(strategy_steps) + 1)
            action = item.get("next_action")
            suggestions = [
                str(value).strip() for value in item.get("suggestions", [])
                if str(value).strip()
            ][:3]
            if action == "re_research":
                queries = [
                    str(value).strip() for value in item.get("search_queries", [])
                    if str(value).strip()
                ][:3]
                detail = f"围绕 {'、'.join(queries)} 补充证据" if queries else "补充缺失证据"
            elif action == "revise":
                detail = (
                    f"按 {'；'.join(suggestions)} 定向修订"
                    if suggestions else "针对逻辑、引用和表达问题定向修订"
                )
            elif action == "complete":
                detail = "通过质量门禁并固化当前研究路径"
            else:
                detail = "达到审核上限，保留未解决风险供后续任务规避"
            strategy_steps.append(f"第{round_number}轮：{detail}")
        if not strategy_steps:
            suggestions = [
                str(item.get("suggestion", "")).strip()
                for item in feedback
                if str(item.get("suggestion", "")).strip()
            ][:5]
            strategy_steps.append(
                "按 Critic 建议执行：" + ("；".join(suggestions) or "补齐证据并定向修订")
            )

        gate_passed = bool(state.get("quality_gate_passed", False))
        outcome = (
            f"评分 {first_score:g}→{final_score:g}；"
            if first_score is not None else f"最终评分 {final_score:g}；"
        ) + (
            "通过发布门禁" if gate_passed else
            "未通过门禁，风险原因：" + "、".join(
                state.get("quality_gate_reasons", []) or ["unknown"]
            )
        )
        critic_summary = "；".join(
            str(item.get("description") or item.get("suggestion") or "").strip()
            for item in feedback
            if str(item.get("description") or item.get("suggestion") or "").strip()
        )[:4000] or None

        return ExperienceMemoryInput(
            scenario=str(state.get("query", "")),
            trigger=trigger,
            strategy="；".join(strategy_steps),
            outcome=outcome,
            critic_summary=critic_summary,
            initial_score=first_score,
            final_score=final_score,
            run_id=run_id,
            metadata={
                "review_rounds": len(history),
                "issue_profile": dict(issue_counter),
                "actions": [item.get("next_action") for item in history],
                "quality_gate_passed": gate_passed,
            },
        )
