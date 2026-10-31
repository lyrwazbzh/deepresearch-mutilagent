"""Evaluate one base/adapter build and enforce the held-out release gate."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.eval.schemas import EvaluationReport, RoutingMetrics
from src.eval.tool_router_eval import evaluate, load_records, release_gate
from src.train.chat_format import format_trajectory_messages, load_openai_tools


def sha256_file(path: str) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def load_baseline(path: str | None) -> RoutingMetrics | None:
    if not path:
        return None
    return EvaluationReport.model_validate_json(
        Path(path).read_text(encoding="utf-8")
    ).metrics


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", default="Qwen/Qwen2.5-7B-Instruct")
    parser.add_argument("--adapter")
    parser.add_argument("--eval-data", required=True)
    parser.add_argument("--tool-schema", default="config/deepresearch_tools.v1.json")
    parser.add_argument("--output", required=True)
    parser.add_argument("--baseline-result")
    parser.add_argument("--min-tool-accuracy", type=float, default=0.90)
    parser.add_argument("--min-schema-validity", type=float, default=0.98)
    parser.add_argument("--max-regression", type=float, default=0.01)
    parser.add_argument("--max-new-tokens", type=int, default=384)
    parser.add_argument("--report-only", action="store_true")
    args = parser.parse_args()

    import torch
    from peft import PeftModel
    from transformers import AutoModelForCausalLM, AutoTokenizer, BitsAndBytesConfig

    if not torch.cuda.is_available():
        raise RuntimeError("tool-router evaluation requires a CUDA device")
    dtype = torch.bfloat16 if torch.cuda.is_bf16_supported() else torch.float16
    tokenizer = AutoTokenizer.from_pretrained(args.model, trust_remote_code=True)
    tools = load_openai_tools(args.tool_schema)
    base = AutoModelForCausalLM.from_pretrained(
        args.model,
        trust_remote_code=True,
        device_map="auto",
        quantization_config=BitsAndBytesConfig(
            load_in_4bit=True,
            bnb_4bit_quant_type="nf4",
            bnb_4bit_use_double_quant=True,
            bnb_4bit_compute_dtype=dtype,
        ),
    )
    model = PeftModel.from_pretrained(base, args.adapter) if args.adapter else base
    model.eval()

    def generate(prefix):
        messages = format_trajectory_messages(prefix)
        prompt = tokenizer.apply_chat_template(
            messages, tools=tools, tokenize=False, add_generation_prompt=True
        )
        inputs = tokenizer(prompt, return_tensors="pt").to(model.device)
        with torch.inference_mode():
            outputs = model.generate(
                **inputs,
                do_sample=False,
                max_new_tokens=args.max_new_tokens,
                pad_token_id=tokenizer.eos_token_id,
            )
        return tokenizer.decode(
            outputs[0][inputs["input_ids"].shape[1]:], skip_special_tokens=False
        )

    metrics, steps = await evaluate(
        load_records(args.eval_data), generate, tool_schema_path=args.tool_schema
    )
    report = EvaluationReport(
        model=args.model,
        adapter=args.adapter,
        dataset=args.eval_data,
        tool_schema_sha256=sha256_file(args.tool_schema),
        metrics=metrics,
        steps=steps,
    )
    Path(args.output).write_text(report.model_dump_json(indent=2), encoding="utf-8")
    gate = release_gate(
        metrics,
        baseline=load_baseline(args.baseline_result),
        min_tool_accuracy=args.min_tool_accuracy,
        min_schema_validity=args.min_schema_validity,
        max_regression=args.max_regression,
    )
    print(json.dumps({"metrics": metrics.model_dump(), "gate": gate}, indent=2))
    return 0 if args.report_only or gate["passed"] else 2


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
