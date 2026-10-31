import json
from pathlib import Path

import pytest

from config.llm_config import get_config

# 真 LLM 评测：依赖外部 LLM 服务与有效 API key，默认跳过（T27 needs_infra）
pytestmark = pytest.mark.needs_infra
from service.deep_research_v2.agents.architect import ChiefArchitect
from service.deep_research_v2.state import (
    ResearchPhase,
    create_initial_state,
)


CASES_PATH = Path(__file__).parent / "cases" / "outline_planning_cases.json"
CASES = json.loads(CASES_PATH.read_text(encoding="utf-8"))


def _assert_unique_nonblank_ids(items):
    identifiers = [item.get("id", "").strip() for item in items]
    assert all(identifiers)
    assert len(identifiers) == len(set(identifiers))


@pytest.mark.llm_eval
@pytest.mark.parametrize("case", CASES, ids=lambda case: case["id"])
@pytest.mark.asyncio
async def test_live_outline_and_search_plan_quality(case):
    config = get_config()
    if not config.api_key:
        pytest.skip("DASHSCOPE_API_KEY is not configured; live LLM eval skipped")

    architect = ChiefArchitect(
        llm_api_key=config.api_key,
        llm_base_url=config.base_url,
        model=config.agents.architect.model,
    )
    state = create_initial_state(case["query"], f"eval-{case['id']}")

    state = await architect.process(state)
    sections = state.get("outline", [])
    questions = state.get("research_questions", [])

    assert state["phase"] == ResearchPhase.AWAITING_OUTLINE_APPROVAL.value
    assert 5 <= len(sections) <= 8
    assert 3 <= len(questions) <= 6
    _assert_unique_nonblank_ids(sections)
    _assert_unique_nonblank_ids(questions)
    assert all(section.get("title", "").strip() for section in sections)
    assert all(section.get("description", "").strip() for section in sections)
    assert all(question.get("text", "").strip() for question in questions)

    state["phase"] = ResearchPhase.PLANNING.value
    state = await architect.process(state)

    sections_with_two_queries = 0
    for section in state.get("outline", []):
        queries = [
            query.strip()
            for query in section.get("search_queries", [])
            if query.strip()
        ]
        if len(set(queries)) >= 2:
            sections_with_two_queries += 1

    assert state["phase"] == ResearchPhase.RESEARCHING.value
    assert sections_with_two_queries / len(sections) >= 0.90
