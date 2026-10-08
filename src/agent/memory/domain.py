from __future__ import annotations

import hashlib
import math
import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

MemoryCategory = Literal[
    "research_preference",
    "research_background",
    "research_method",
    "output_preference",
    "long_term_goal",
    "collaboration_context",
]
MemorySource = Literal[
    "explicit_remember",
    "turn_completion",
    "explicit_correction",
    "manual_edit",
]
MemoryStatus = Literal["active", "pending_conflict", "superseded", "forgotten"]


@dataclass(frozen=True)
class TopicDefinition:
    topic: str
    category: MemoryCategory
    base_memory: bool
    aliases: tuple[str, ...] = ()


class TopicRegistry:
    """Versioned, code-shipped registry of the bounded Research LTM namespace."""

    version = "memory-topics-v1"

    def __init__(self, definitions: tuple[TopicDefinition, ...] | None = None) -> None:
        self.definitions = definitions or DEFAULT_TOPICS
        self._by_topic = {item.topic: item for item in self.definitions}
        self._aliases: dict[str, str] = {}
        for item in self.definitions:
            for alias in (item.topic, *item.aliases):
                self._aliases[_normalize_topic_text(alias)] = item.topic

    def get(self, topic: str) -> TopicDefinition:
        canonical = self.normalize(topic)
        return self._by_topic[canonical]

    def normalize(self, topic: str) -> str:
        normalized = _normalize_topic_text(topic)
        canonical = self._aliases.get(normalized)
        if canonical is None:
            raise ValueError(f"Unknown or disallowed memory topic: {topic}")
        return canonical

    def topics_for_query(self, query: str) -> set[str]:
        normalized = _normalize_topic_text(query)
        matches: set[str] = set()
        for alias, canonical in self._aliases.items():
            if alias and (alias == normalized or alias in normalized):
                matches.add(canonical)
        return matches


DEFAULT_TOPICS: tuple[TopicDefinition, ...] = (
    TopicDefinition(
        "output.language.default",
        "output_preference",
        True,
        ("output language", "language preference", "输出语言", "默认语言"),
    ),
    TopicDefinition(
        "output.detail.default",
        "output_preference",
        True,
        ("detail level", "answer detail", "输出详略", "详细程度"),
    ),
    TopicDefinition(
        "research.source.preference",
        "research_preference",
        True,
        ("source preference", "preferred sources", "来源偏好", "原始论文"),
    ),
    TopicDefinition(
        "research.scope.default",
        "research_preference",
        False,
        ("research scope", "研究范围", "默认范围"),
    ),
    TopicDefinition(
        "research.method.validation",
        "research_method",
        True,
        ("validation method", "research validation", "验证方法", "交叉验证"),
    ),
    TopicDefinition(
        "background.user.industry",
        "research_background",
        False,
        ("user industry", "industry background", "行业背景", "所在行业"),
    ),
    TopicDefinition(
        "background.user.role",
        "research_background",
        False,
        ("user role", "professional role", "用户角色", "职业角色"),
    ),
    TopicDefinition(
        "goal.long_term.primary",
        "long_term_goal",
        True,
        ("long term goal", "primary goal", "长期目标", "长期研究目标"),
    ),
    TopicDefinition(
        "collaboration.confirmation.default",
        "collaboration_context",
        True,
        ("confirmation preference", "确认习惯", "先确认"),
    ),
    TopicDefinition(
        "collaboration.document_workspace_boundary",
        "collaboration_context",
        True,
        ("document workspace boundary", "文档工作区边界", "知识库工作区边界"),
    ),
)


class MemoryCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    topic: str = Field(min_length=1)
    category: MemoryCategory
    content: str = Field(min_length=1)
    confidence: float = Field(ge=0, le=1)
    supporting_user_message_ids: list[str] = Field(min_length=1)


class ExtractedMemoryBatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    candidates: list[MemoryCandidate] = Field(default_factory=list, max_length=5)


class MemoryRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    memory_id: str
    tenant_id: str
    user_id: str
    topic: str
    category: MemoryCategory
    content: str
    source: MemorySource
    supporting_message_ids: list[str]
    confidence: float
    content_hash: str
    write_key: str
    status: MemoryStatus
    related_memory_id: str | None = None
    revision: int
    embedding_status: Literal["pending", "ready"]
    created_at: datetime
    updated_at: datetime


class RememberRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    topic: str
    content: str
    supporting_message_ids: list[str] = Field(min_length=1)


class ManualEditRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_revision: int = Field(ge=1)
    topic: str | None = None
    content: str
    supporting_message_ids: list[str] = Field(default_factory=list)


class ResolveMemoryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: Literal["accept_new", "keep_old"]
    expected_revision: int = Field(ge=1)
    related_expected_revision: int = Field(ge=1)


class MemoryWriteResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    memory: MemoryRecord
    disposition: Literal["created", "exact_dedup", "replacement", "pending_conflict"]


@dataclass(frozen=True)
class RecallScore:
    memory_id: str
    base_memory: bool
    exact_topic: bool
    semantic_score: float
    lexical_score: float
    relevance_score: float


def canonical_memory_content(content: str) -> str:
    normalized = unicodedata.normalize("NFKC", content).strip()
    normalized = re.sub(r"\s+", " ", normalized)
    normalized = re.sub(r"\s*([,.;:!?，。；：！？、])\s*", r"\1", normalized)
    return normalized


def memory_content_hash(content: str) -> str:
    return hashlib.sha256(canonical_memory_content(content).encode("utf-8")).hexdigest()


def memory_write_key(
    *,
    tenant_id: str,
    user_id: str,
    source: MemorySource,
    topic: str,
    content_hash: str,
    supporting_message_ids: list[str],
    logical_key: str | None = None,
) -> str:
    identity = logical_key or "|".join(sorted(set(supporting_message_ids)))
    # The logical operation identity intentionally excludes content so a retry
    # with changed payload is detected as an idempotency conflict.
    value = f"{tenant_id}|{user_id}|{source}|{topic}|{identity}"
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def lexical_tokens(text: str) -> set[str]:
    normalized = unicodedata.normalize("NFKC", text).casefold()
    tokens = set(re.findall(r"[a-z0-9]+", normalized))
    runs = re.findall(r"[\u3400-\u4dbf\u4e00-\u9fff]+", normalized)
    for run in runs:
        tokens.update(run[index : index + 2] for index in range(max(0, len(run) - 1)))
        tokens.update(char for char in run if char in {"不", "无", "非", "未", "没"})
    return tokens


def lexical_score(query: str, memory: str) -> float:
    query_tokens = lexical_tokens(query)
    memory_tokens = lexical_tokens(memory)
    if not query_tokens or not memory_tokens:
        return 0.0
    return len(query_tokens & memory_tokens) / math.sqrt(
        len(query_tokens) * len(memory_tokens)
    )


def cosine_similarity(left: list[float] | None, right: list[float] | None) -> float:
    if not left or not right or len(left) != len(right):
        return 0.0
    denominator = math.sqrt(sum(item * item for item in left)) * math.sqrt(
        sum(item * item for item in right)
    )
    if denominator == 0:
        return 0.0
    return max(-1.0, min(1.0, sum(a * b for a, b in zip(left, right)) / denominator))


def _normalize_topic_text(value: str) -> str:
    value = unicodedata.normalize("NFKC", value).strip().casefold()
    return re.sub(r"[\s_\-/]+", " ", value).strip()
