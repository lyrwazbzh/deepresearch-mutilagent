"""Expand a small reviewed seed set into unique multi-domain synthesis inputs."""

from __future__ import annotations

import argparse
import hashlib
import itertools
import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from openai import OpenAI

from src.data.seed_loader import SeedQuestion, load_seed_questions


def expansion_prompt(seed: SeedQuestion, count: int) -> str:
    return f"""基于种子问题生成 {count} 个彼此不同、真实自然的深度研究问题。
保持行业与核心意图，但改变主体、地区、时间范围、比较维度或决策场景；禁止虚构个人信息。

种子问题：{seed.question}
行业：{seed.industry}
问题类型：{seed.question_type}

只输出 JSON：{{"questions":[{{"question":"...","difficulty":"easy|medium|hard"}}]}}"""


def stable_id(parent_id: str, question: str) -> str:
    digest = hashlib.sha256(f"{parent_id}|{question}".encode("utf-8")).hexdigest()[:20]
    return f"seed_{digest}"


def generate_batch(seed, *, count, api_key, base_url, model):
    client = OpenAI(api_key=api_key, base_url=base_url)
    response = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": "你生成用于工具路由训练的多样化研究问题，只输出合法 JSON。"},
            {"role": "user", "content": expansion_prompt(seed, count)},
        ],
        response_format={"type": "json_object"},
        temperature=0.9,
        max_tokens=3000,
        timeout=60,
    )
    payload = json.loads(response.choices[0].message.content or "{}")
    results = []
    for item in payload.get("questions", []):
        question = str(item.get("question", "")).strip()
        if len(question) < 3:
            continue
        results.append(SeedQuestion(
            id=stable_id(seed.id, question),
            question=question,
            industry=seed.industry,
            question_type=seed.question_type,
            difficulty=item.get("difficulty", seed.difficulty),
            expected_tools=seed.expected_tools,
            parent_seed_id=seed.id,
        ))
    return results


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--target", type=int, default=6500)
    parser.add_argument("--batch-size", type=int, default=12)
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--model", default="qwen-plus")
    parser.add_argument("--base-url", default="https://dashscope.aliyuncs.com/compatible-mode/v1")
    parser.add_argument("--llm-keys", default="")
    args = parser.parse_args()

    keys = [value.strip() for value in (args.llm_keys or os.getenv("LLM_KEYS", "")).split(",") if value.strip()]
    if not keys:
        raise SystemExit("provide API keys through --llm-keys or LLM_KEYS")
    parents = load_seed_questions(args.input)
    if not parents:
        raise SystemExit("input contains no seeds")

    output = Path(args.output)
    existing = load_seed_questions(output) if output.exists() else []
    by_id = {seed.id: seed for seed in [*parents, *existing]}
    remaining = max(args.target - len(by_id), 0)
    round_count = (remaining + args.batch_size - 1) // args.batch_size
    jobs = []
    key_cycle = itertools.cycle(keys)
    parent_cycle = itertools.cycle(parents)
    for _ in range(round_count):
        jobs.append((next(parent_cycle), next(key_cycle)))

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = [
            pool.submit(
                generate_batch,
                seed,
                count=args.batch_size,
                api_key=key,
                base_url=args.base_url,
                model=args.model,
            )
            for seed, key in jobs
        ]
        for future in as_completed(futures):
            try:
                for seed in future.result():
                    by_id.setdefault(seed.id, seed)
            except Exception as exc:
                print(f"expansion batch failed: {type(exc).__name__}")

    rows = list(by_id.values())[:args.target]
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        "\n".join(item.model_dump_json(exclude_none=True) for item in rows) + "\n",
        encoding="utf-8",
    )
    print({"unique_seeds": len(rows), "target": args.target})
    if len(rows) < args.target:
        raise SystemExit("target not reached; rerun to fill failed or duplicate batches")


if __name__ == "__main__":
    main()
