import json

import pytest

from src.data.schema_v3 import Trajectory
from src.eval.parser import parse_tool_calls
from src.eval.tool_router_eval import evaluate, release_gate


def trajectory():
    return Trajectory.model_validate({
        "messages": [
            {"role": "system", "content": "route"},
            {"role": "user", "content": "检索储能市场"},
            {"role": "assistant", "tool_calls": [{
                "id": "call_1",
                "type": "function",
                "function": {
                    "name": "web_search",
                    "arguments": json.dumps({"query": "储能市场"}, ensure_ascii=False),
                },
            }]},
            {"role": "tool", "tool_call_id": "call_1", "content": "ok"},
        ],
        "metadata": {
            "trajectory_id": "t1", "seed_id": "s1", "industry": "energy",
            "question_type": "market", "teacher_model": "teacher",
        },
    })


def test_parser_accepts_qwen_tool_block():
    calls = parse_tool_calls(
        '<tool_call>{"name":"web_search","arguments":{"query":"储能"}}</tool_call>'
    )
    assert calls[0].name == "web_search"


@pytest.mark.asyncio
async def test_eval_and_release_gate():
    async def generate(_):
        return {"name": "web_search", "arguments": {"query": "储能市场"}}

    metrics, steps = await evaluate(
        [trajectory()], generate, tool_schema_path="config/deepresearch_tools.v1.json"
    )
    assert metrics.tool_selection_accuracy == 1.0
    assert metrics.argument_schema_validity == 1.0
    assert release_gate(metrics)["passed"] is True
    assert steps[0].parse_error is None
