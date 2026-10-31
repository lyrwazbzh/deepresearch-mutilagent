"""Run deterministic offline report evaluation from JSONL fixtures."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from evals.runner import EvaluationRunner


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--baseline")
    parser.add_argument("--max-regression", type=float, default=0.01)
    args = parser.parse_args()

    runner = EvaluationRunner()
    results = await runner.evaluate(runner.load_jsonl(args.input))
    runner.write_jsonl(args.output, results)
    payload = {"summary": runner.summary(results)}
    exit_code = 0
    if args.baseline:
        baseline = [
            __import__("evals.schemas", fromlist=["EvalResult"]).EvalResult.model_validate_json(line)
            for line in Path(args.baseline).read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        payload["gate"] = runner.compare_and_gate(
            baseline, results, max_regression=args.max_regression
        )
        exit_code = 0 if payload["gate"]["passed"] else 2
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
