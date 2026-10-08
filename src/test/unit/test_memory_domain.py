from __future__ import annotations

import math

import pytest

from src.agent.memory.domain import (
    TopicRegistry,
    canonical_memory_content,
    lexical_score,
    lexical_tokens,
    memory_content_hash,
    memory_write_key,
)
from src.agent.backends.persistence.models import ConversationMessageRow
from src.agent.backends.persistence.models import ResearchMemoryRow
from src.agent.task_service import QueryCompletionService


def test_topic_registry_normalizes_aliases_and_rejects_unknown_topics() -> None:
    registry = TopicRegistry()

    assert registry.normalize("来源偏好") == "research.source.preference"
    assert registry.get("output language").category == "output_preference"
    assert registry.get("output.language.default").base_memory is True
    assert registry.topics_for_query("Please use my output language preference") == {
        "output.language.default"
    }
    with pytest.raises(ValueError):
        registry.normalize("unbounded.topic.created.by.model")


def test_memory_content_identity_is_nfkc_whitespace_and_punctuation_normalized() -> None:
    left = "  Use  primary papers ， not blogs。 "
    right = "Use primary papers,not blogs。"

    assert canonical_memory_content(left) == right
    assert memory_content_hash(left) == memory_content_hash(right)
    assert memory_write_key(
        tenant_id="tenant",
        user_id="user",
        source="explicit_remember",
        topic="research.source.preference",
        content_hash=memory_content_hash(left),
        supporting_message_ids=["m1"],
        logical_key="command-1",
    ) == memory_write_key(
        tenant_id="tenant",
        user_id="user",
        source="explicit_remember",
        topic="research.source.preference",
        content_hash=memory_content_hash(right),
        supporting_message_ids=["m1"],
        logical_key="command-1",
    )


def test_lexical_tokenizer_uses_words_and_cjk_bigrams_with_negation_and_numbers() -> None:
    assert {"not", "2026", "papers"} <= lexical_tokens("not 2026 papers")
    assert {"不要", "要博", "博客", "不"} <= lexical_tokens("不要博客")
    assert lexical_score("", "anything") == 0
    expected = 1 / math.sqrt(2)
    assert lexical_score("alpha beta", "beta") == pytest.approx(expected)


def test_query_completion_prioritizes_current_input_and_obeys_count_and_token_bounds() -> None:
    prior = [
        ConversationMessageRow(
            message_id=f"m{index}",
            tenant_id="tenant",
            user_id="user",
            thread_id="thread",
            role="user" if index % 2 == 0 else "assistant",
            content=f"history-{index}-" + ("x" * 24),
        )
        for index in range(6)
    ]
    service = QueryCompletionService(message_limit=2, token_budget=40)
    result = service.complete(prior, "CURRENT REQUEST")

    assert result.endswith("CURRENT REQUEST")
    assert "history-0" not in result
    assert sum(f"history-{index}" in result for index in range(6)) <= 2
    assert service._tokens(result) <= 40

    current_only = QueryCompletionService(message_limit=12, token_budget=5).complete(
        prior, "prefix-" + ("current" * 20)
    )
    assert current_only.endswith("current")
    assert "history" not in current_only
    assert QueryCompletionService._tokens(current_only) <= 5


def test_research_memories_is_one_table_with_formal_partial_uniqueness() -> None:
    table = ResearchMemoryRow.__table__
    assert set(table.columns.keys()) == {
        "memory_id",
        "tenant_id",
        "user_id",
        "topic",
        "content",
        "source",
        "supporting_message_ids",
        "confidence",
        "embedding",
        "content_hash",
        "write_key",
        "status",
        "related_memory_id",
        "revision",
        "created_at",
        "updated_at",
    }
    indexes = {index.name: index for index in table.indexes}
    assert indexes["uq_memory_topic_content_live"].unique is True
    assert indexes["uq_memory_active_topic"].unique is True
    assert "ix_memories_embedding_hnsw" in indexes
    assert all(
        name not in ResearchMemoryRow.metadata.tables
        for name in ("memory_versions", "memory_conflicts", "memory_embeddings")
    )
