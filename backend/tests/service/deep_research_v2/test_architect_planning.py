import json
from unittest.mock import AsyncMock

import pytest

from service.deep_research_v2.agents.architect import ChiefArchitect
from service.deep_research_v2.state import ResearchPhase, create_initial_state


@pytest.fixture
def architect():
    return ChiefArchitect(
        llm_api_key="test-key",
        llm_base_url="https://example.com/v1",
    )


def _sections(count: int = 5):
    return [
        {
            "id": f"section_{index}",
            "title": f"章节 {index}",
            "description": f"研究章节 {index} 的关键数据与趋势",
        }
        for index in range(1, count + 1)
    ]


def _questions(count: int = 3):
    return [
        {"id": f"question_{index}", "text": f"核心问题 {index}"}
        for index in range(1, count + 1)
    ]


@pytest.mark.asyncio
async def test_init_generates_only_editable_plan(architect):
    state = create_initial_state("中国机器人产业研究", "session-1")
    architect.call_llm = AsyncMock(
        return_value=json.dumps(
            {"sections": _sections(), "research_questions": _questions()},
            ensure_ascii=False,
        )
    )

    result = await architect.process(state)

    assert result["phase"] == ResearchPhase.AWAITING_OUTLINE_APPROVAL.value
    assert result["outline_revision"]
    assert len(result["outline"]) == 5
    assert all(section["search_queries"] == [] for section in result["outline"])
    assert result["research_questions"] == _questions()
    assert result["hypotheses"] == []


@pytest.mark.asyncio
async def test_planning_merges_queries_by_stable_id(architect):
    state = create_initial_state("中国机器人产业研究", "session-2")
    state["phase"] = ResearchPhase.PLANNING.value
    state["outline"] = _sections()
    state["research_questions"] = _questions()
    architect.call_llm = AsyncMock(
        return_value=json.dumps(
            {
                "sections": [
                    {
                        "id": section["id"],
                        "queries": [f"{section['title']} 2026 市场数据"],
                    }
                    for section in state["outline"]
                ],
                "hypotheses": [
                    {"id": "hypothesis_1", "content": "行业集中度将继续提升"}
                ],
            },
            ensure_ascii=False,
        )
    )

    result = await architect.process(state)

    assert result["phase"] == ResearchPhase.RESEARCHING.value
    assert [section["id"] for section in result["outline"]] == [
        f"section_{index}" for index in range(1, 6)
    ]
    assert all(section["search_queries"] for section in result["outline"])
    assert result["hypotheses"][0]["id"] == "hypothesis_1"


@pytest.mark.asyncio
async def test_planning_falls_back_when_at_least_three_sections_are_valid(architect):
    state = create_initial_state("中国机器人产业研究", "session-3")
    state["phase"] = ResearchPhase.PLANNING.value
    state["outline"] = _sections()
    state["research_questions"] = _questions()
    partial = {
        "sections": [
            {"id": f"section_{index}", "queries": [f"有效关键词 {index}"]}
            for index in range(1, 4)
        ],
        "hypotheses": [],
    }
    architect.call_llm = AsyncMock(
        side_effect=[
            json.dumps({"sections": [], "hypotheses": []}),
            json.dumps(partial, ensure_ascii=False),
        ]
    )

    result = await architect.process(state)

    assert architect.call_llm.await_count == 2
    assert result["phase"] == ResearchPhase.RESEARCHING.value
    assert result["outline"][3]["search_queries"] == [
        "中国机器人产业研究 章节 4",
        "章节 4 研究章节 4 的关键数据与趋势",
    ]


@pytest.mark.asyncio
async def test_planning_stays_put_when_fewer_than_three_sections_are_valid(architect):
    state = create_initial_state("中国机器人产业研究", "session-4")
    state["phase"] = ResearchPhase.PLANNING.value
    state["outline"] = _sections()
    state["research_questions"] = _questions()
    insufficient = {
        "sections": [
            {"id": "section_1", "queries": ["有效关键词 1"]},
            {"id": "section_2", "queries": ["有效关键词 2"]},
        ],
        "hypotheses": [],
    }
    architect.call_llm = AsyncMock(
        return_value=json.dumps(insufficient, ensure_ascii=False)
    )

    result = await architect.process(state)

    assert architect.call_llm.await_count == 2
    assert result["phase"] == ResearchPhase.PLANNING.value
    assert "关键词" in result["errors"][-1]
