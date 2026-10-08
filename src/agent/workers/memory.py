from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Protocol

from sqlalchemy import and_, or_, select

from src.agent.memory.domain import ExtractedMemoryBatch
from src.agent.schema.model_config import FrozenModelBinding, ModelBindingSnapshot
from src.agent.memory.service import MemoryService
from src.agent.backends.persistence.database import SessionFactory
from src.agent.backends.persistence.models import ConversationMessageRow, ResearchMemoryRow, ResearchTaskRow

EXTRACTOR_VERSION = "research-ltm-extractor-v1"
PROMPT_VERSION = "research-ltm-prompt-v1"


class MemoryModelRuntime(Protocol):
    def build_chat_model_for(
        self, tenant_id: str, binding: FrozenModelBinding, role: str
    ) -> Any: ...

    def embed(
        self, tenant_id: str, binding: FrozenModelBinding, texts: list[str]
    ) -> list[list[float]]: ...


class MemoryExtractor:
    def extract(
        self,
        *,
        tenant_id: str,
        binding: FrozenModelBinding,
        messages: list[ConversationMessageRow],
        model_runtime: MemoryModelRuntime,
    ) -> ExtractedMemoryBatch:
        model = model_runtime.build_chat_model_for(
            tenant_id, binding, "memory_extractor"
        )
        transcript = "\n".join(
            f"[{item.message_id}] {item.role}: {item.content}" for item in messages
        )
        prompt = f"""Role: Research LTM Memory Extractor
Prompt version: {PROMPT_VERSION}
Extract at most five stable, cross-task user research collaboration memories.
Only user messages are factual support. Assistant messages provide context only.
Allowed categories: research_preference, research_background, research_method,
output_preference, long_term_goal, collaboration_context.
Use a canonical/normalizable topic and include supporting_user_message_ids.
Reject temporary task requirements, research facts, tool results and assistant inferences.
Return structured JSON matching ExtractedMemoryBatch.

Messages:
{transcript}
"""
        return model.invoke_structured(prompt, ExtractedMemoryBatch)


class MemoryExtractionWorker:
    def __init__(
        self,
        session_factory: SessionFactory,
        memory_service: MemoryService,
        model_runtime: MemoryModelRuntime,
        extractor: MemoryExtractor | None = None,
    ) -> None:
        self.session_factory = session_factory
        self.memory_service = memory_service
        self.model_runtime = model_runtime
        self.extractor = extractor or MemoryExtractor()

    def run(self, payload: dict[str, Any]) -> int:
        tenant_id = str(payload["tenant_id"])
        user_id = str(payload["user_id"])
        thread_id = str(payload["thread_id"])
        task_id = str(payload["task_id"])
        assistant_message_id = str(payload["assistant_message_id"])
        with self.session_factory() as session:
            task = session.scalar(
                select(ResearchTaskRow).where(
                    ResearchTaskRow.task_id == task_id,
                    ResearchTaskRow.tenant_id == tenant_id,
                    ResearchTaskRow.user_id == user_id,
                )
            )
            if task is None:
                return 0
            cutoff = session.scalar(
                select(ConversationMessageRow).where(
                    ConversationMessageRow.tenant_id == tenant_id,
                    ConversationMessageRow.user_id == user_id,
                    ConversationMessageRow.thread_id == thread_id,
                    ConversationMessageRow.message_id == assistant_message_id,
                    ConversationMessageRow.role == "assistant",
                )
            )
            if cutoff is None:
                return 0
            rows = list(
                session.scalars(
                    select(ConversationMessageRow)
                    .where(
                        ConversationMessageRow.tenant_id == tenant_id,
                        ConversationMessageRow.user_id == user_id,
                        ConversationMessageRow.thread_id == thread_id,
                        ConversationMessageRow.role.in_(("user", "assistant")),
                        or_(
                            ConversationMessageRow.created_at < cutoff.created_at,
                            and_(
                                ConversationMessageRow.created_at == cutoff.created_at,
                                ConversationMessageRow.message_id <= cutoff.message_id,
                            ),
                        ),
                    )
                    .order_by(
                        ConversationMessageRow.created_at.desc(),
                        ConversationMessageRow.message_id.desc(),
                    )
                    .limit(20)
                ).all()
            )
            rows.reverse()
            binding = ModelBindingSnapshot.model_validate(task.model_binding_snapshot).get(
                "reasoning"
            )
        messages = _bounded_messages(rows)
        user_messages = {
            item.message_id: item.content
            for item in messages
            if item.role == "user" and _durable_user_message(item.content)
        }
        if not user_messages:
            return 0
        messages = [
            item
            for item in messages
            if item.role == "assistant" or item.message_id in user_messages
        ]
        identity_payload = [
            {"message_id": item.message_id, "role": item.role, "content": item.content}
            for item in messages
        ]
        input_hash = hashlib.sha256(
            json.dumps(
                identity_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            ).encode("utf-8")
        ).hexdigest()
        batch = self.extractor.extract(
            tenant_id=tenant_id,
            binding=binding,
            messages=messages,
            model_runtime=self.model_runtime,
        )
        results = self.memory_service.extract_batch(
            tenant_id=tenant_id,
            user_id=user_id,
            candidates=batch.candidates[:5],
            user_messages=user_messages,
            input_hash=input_hash,
        )
        return len(results)


class MemoryEmbeddingWorker:
    def __init__(
        self,
        session_factory: SessionFactory,
        memory_service: MemoryService,
        model_runtime: MemoryModelRuntime,
    ) -> None:
        self.session_factory = session_factory
        self.memory_service = memory_service
        self.model_runtime = model_runtime

    def run(self, payload: dict[str, Any]) -> bool:
        tenant_id = str(payload["tenant_id"])
        user_id = str(payload["user_id"])
        memory_id = str(payload["memory_id"])
        revision = int(payload["revision"])
        binding = FrozenModelBinding.model_validate(payload["binding"])
        with self.session_factory() as session:
            row = session.scalar(
                select(ResearchMemoryRow).where(
                    ResearchMemoryRow.tenant_id == tenant_id,
                    ResearchMemoryRow.user_id == user_id,
                    ResearchMemoryRow.memory_id == memory_id,
                    ResearchMemoryRow.revision == revision,
                    ResearchMemoryRow.status.in_(("active", "pending_conflict")),
                    ResearchMemoryRow.embedding.is_(None),
                )
            )
            if row is None:
                return False
            content = row.content
        vector = self.model_runtime.embed(tenant_id, binding, [content])[0]
        return self.memory_service.set_embedding(
            tenant_id=tenant_id,
            user_id=user_id,
            memory_id=memory_id,
            expected_revision=revision,
            embedding=vector,
        )


def _bounded_messages(
    messages: list[ConversationMessageRow], token_budget: int = 4_000
) -> list[ConversationMessageRow]:
    selected: list[ConversationMessageRow] = []
    used = 0
    for message in reversed(messages[-20:]):
        cost = max(1, (len(message.content) + 3) // 4)
        if used + cost > token_budget:
            continue
        selected.append(message)
        used += cost
    selected.reverse()
    return selected


def _durable_user_message(content: str) -> bool:
    normalized = " ".join(content.split()).strip()
    if not normalized or len(normalized) > 8_000:
        return False
    if re.fullmatch(r"(?i)(ok|okay|yes|no|thanks|thank you|好的|好|是|不是|谢谢)[.!。！]?", normalized):
        return False
    temporary_markers = (
        "this time",
        "for this task",
        "for this request",
        "temporarily",
        "这次",
        "本次",
        "这个任务",
        "临时",
    )
    return not any(marker in normalized.casefold() for marker in temporary_markers)
