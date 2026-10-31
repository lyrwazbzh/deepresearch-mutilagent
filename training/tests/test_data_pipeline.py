import json
from pathlib import Path

from src.data.pipeline import DatasetBuilder


ROOT = Path(__file__).resolve().parents[1]


def make_trajectory(seed_id: str, question: str, call_id: str = "call-1") -> dict:
    return {
        "messages": [
            {"role": "system", "content": "route tools"},
            {"role": "user", "content": question},
            {
                "role": "assistant",
                "tool_calls": [
                    {
                        "id": call_id,
                        "type": "function",
                        "function": {
                            "name": "web_search",
                            "arguments": json.dumps({"query": question}),
                        },
                    }
                ],
            },
            {"role": "tool", "tool_call_id": call_id, "content": "result"},
            {"role": "assistant", "content": "done"},
        ],
        "metadata": {
            "trajectory_id": f"trajectory-{seed_id}",
            "seed_id": seed_id,
            "industry": "automotive",
            "question_type": "trend",
            "teacher_model": "teacher",
        },
    }


def test_pipeline_validates_sanitizes_deduplicates_and_groups(tmp_path):
    builder = DatasetBuilder(
        tool_schema_path=ROOT / "config" / "deepresearch_tools.v1.json",
        min_quality=0.7,
    )
    rows = [
        make_trajectory("seed-a", "动力电池 sk-ABCDEFGHIJKLMNOP 趋势"),
        make_trajectory("seed-b", "动力电池 sk-ABCDEFGHIJKLMNOP 趋势", "call-2"),
        make_trajectory("seed-c", "储能电池价格趋势", "call-3"),
    ]
    manifest = builder.build(rows, output_dir=tmp_path)

    assert manifest["raw_count"] == 3
    assert manifest["valid_count"] == 2
    assert manifest["duplicate_rejected"] == 1
    output = "".join(
        path.read_text(encoding="utf-8")
        for path in (tmp_path / "train.jsonl", tmp_path / "dev.jsonl", tmp_path / "test.jsonl")
    )
    assert "sk-ABCDEFGHIJKLMNOP" not in output
    assert "[REDACTED_API_KEY]" in output
