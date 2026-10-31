"""Synthesize schema-valid tool-routing trajectories with a teacher model.

The script never embeds credentials.  Pass comma-separated keys with
``--llm-keys`` or the ``LLM_KEYS`` environment variable.  Output is appended
idempotently so an interrupted batch can resume without regenerating seeds.
"""

from __future__ import annotations

import argparse
import itertools
import json
import os
import sys
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from openai import OpenAI

from src.data.quality_scorer import score_trajectory
from src.data.schema_v3 import (
    AssistantToolCall,
    FunctionCall,
    Trajectory,
    TrajectoryMessage,
    TrajectoryMetadata,
    load_tool_schema,
    validate_tool_arguments,
)
from src.data.seed_loader import SeedQuestion, load_seed_questions


def teacher_prompt(seed: SeedQuestion, tool_schema: dict[str, Any]) -> str:
    tools = [
        {
            "name": item["name"],
            "description": item["description"],
            "parameters": item["parameters"],
        }
        for item in tool_schema.values()
    ]
    return f"""你是 DeepResearch 工具调用轨迹教师。根据问题生成 1-3 步工具调用。

问题：{seed.question}
行业：{seed.industry}
类型：{seed.question_type}
难度：{seed.difficulty}
可用工具：{json.dumps(tools, ensure_ascii=False)}

严格返回 JSON，不要 Markdown：
{{
  "steps": [
    {{"name": "工具名", "arguments": {{}}, "observation": "该工具可能返回的结构化结果"}}
  ],
  "final_answer": "基于工具结果形成的简短结论"
}}

要求：工具名和参数必须符合给定 schema；不要输出密钥、个人信息或数据库连接串。"""


class TeacherTrajectoryGenerator:
    def __init__(
        self,
        *,
        api_key: str,
        base_url: str,
        model: str,
        tool_schema: dict[str, dict[str, Any]],
    ):
        self.client = OpenAI(api_key=api_key, base_url=base_url)
        self.model = model
        self.tool_schema = tool_schema

    def generate(self, seed: SeedQuestion, batch: str) -> Trajectory:
        response = self.client.chat.completions.create(
            model=self.model,
            messages=[
                {
                    "role": "system",
                    "content": "你只输出合法 JSON，并严格遵守给定工具参数 schema。",
                },
                {"role": "user", "content": teacher_prompt(seed, self.tool_schema)},
            ],
            response_format={"type": "json_object"},
            temperature=0.7,
            max_tokens=2500,
            timeout=60,
        )
        payload = json.loads(response.choices[0].message.content or "{}")
        steps = payload.get("steps")
        if not isinstance(steps, list) or not 1 <= len(steps) <= 3:
            raise ValueError("teacher must return 1-3 steps")

        messages = [
            TrajectoryMessage(
                role="system",
                content="Select tools from the supplied DeepResearch tool schema.",
            ),
            TrajectoryMessage(role="user", content=seed.question),
        ]
        for step in steps:
            call_id = f"call_{uuid.uuid4().hex[:16]}"
            call = AssistantToolCall(
                id=call_id,
                function=FunctionCall(
                    name=step["name"],
                    arguments=json.dumps(
                        step.get("arguments", {}), ensure_ascii=False, sort_keys=True
                    ),
                ),
            )
            messages.append(TrajectoryMessage(role="assistant", tool_calls=[call]))
            messages.append(
                TrajectoryMessage(
                    role="tool",
                    tool_call_id=call_id,
                    content=str(step.get("observation", "")),
                )
            )
        messages.append(
            TrajectoryMessage(
                role="assistant", content=str(payload.get("final_answer", "已完成工具调用。"))
            )
        )
        trajectory = Trajectory(
            messages=messages,
            metadata=TrajectoryMetadata(
                trajectory_id=f"traj_{uuid.uuid4().hex}",
                seed_id=seed.id,
                industry=seed.industry,
                question_type=seed.question_type,
                difficulty=seed.difficulty,
                teacher_model=self.model,
                generation_batch=batch,
                parent_seed_id=seed.parent_seed_id,
            ),
        )
        errors = validate_tool_arguments(trajectory, self.tool_schema)
        if errors:
            raise ValueError("; ".join(errors))
        trajectory.metadata.quality_score = score_trajectory(trajectory)
        trajectory.refresh_content_hash()
        return trajectory


def load_completed_seed_ids(output: Path) -> set[str]:
    if not output.exists():
        return set()
    completed: set[str] = set()
    for line in output.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            completed.add(json.loads(line)["metadata"]["seed_id"])
        except (KeyError, json.JSONDecodeError):
            continue
    return completed


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seed-file", type=Path, required=True)
    parser.add_argument(
        "--tool-schema", type=Path, default=Path("config/deepresearch_tools.v1.json")
    )
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--target-valid", type=int, default=5000)
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--model", default="qwen-plus")
    parser.add_argument(
        "--base-url", default="https://dashscope.aliyuncs.com/compatible-mode/v1"
    )
    parser.add_argument("--llm-keys", default="")
    parser.add_argument("--batch", default="teacher-v1")
    args = parser.parse_args()

    keys = [
        item.strip()
        for item in (args.llm_keys or os.getenv("LLM_KEYS", "")).split(",")
        if item.strip()
    ]
    if not keys:
        raise SystemExit("provide API keys through --llm-keys or LLM_KEYS")

    tool_schema = load_tool_schema(args.tool_schema)
    completed = load_completed_seed_ids(args.output)
    seeds = [seed for seed in load_seed_questions(args.seed_file) if seed.id not in completed]
    remaining = max(args.target_valid - len(completed), 0)
    if remaining == 0:
        print(f"target already reached: {len(completed)}")
        return
    if len(seeds) < remaining:
        raise SystemExit(
            f"need {remaining} unique seeds but only {len(seeds)} are available; expand seeds first"
        )

    args.output.parent.mkdir(parents=True, exist_ok=True)
    write_lock = threading.Lock()
    success = 0
    failures = 0

    def run(item: tuple[SeedQuestion, str]) -> Trajectory:
        seed, key = item
        generator = TeacherTrajectoryGenerator(
            api_key=key,
            base_url=args.base_url,
            model=args.model,
            tool_schema=tool_schema,
        )
        return generator.generate(seed, args.batch)

    key_cycle = itertools.cycle(keys)
    work = [(seed, next(key_cycle)) for seed in seeds[:remaining]]
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = [pool.submit(run, item) for item in work]
        for future in as_completed(futures):
            try:
                trajectory = future.result()
            except Exception as exc:
                failures += 1
                print(f"generation failed: {type(exc).__name__}")
                continue
            with write_lock, args.output.open("a", encoding="utf-8") as handle:
                handle.write(trajectory.model_dump_json(exclude_none=True) + "\n")
                handle.flush()
            success += 1

    print({"generated": success, "failed": failures, "total": len(completed) + success})
    if len(completed) + success < args.target_valid:
        raise SystemExit("target not reached; rerun after fixing failed seeds")


if __name__ == "__main__":
    main()
