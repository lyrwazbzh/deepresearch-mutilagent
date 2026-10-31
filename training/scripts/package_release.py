"""Package a gated adapter, manifest, schema, and evaluation as one release."""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.eval.schemas import EvaluationReport
from src.eval.tool_router_eval import release_gate


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--training-run", required=True)
    parser.add_argument("--evaluation", required=True)
    parser.add_argument("--release-dir", required=True)
    parser.add_argument("--min-tool-accuracy", type=float, default=0.90)
    parser.add_argument("--min-schema-validity", type=float, default=0.98)
    args = parser.parse_args()

    run_dir = Path(args.training_run)
    training_manifest = json.loads(
        (run_dir / "release-manifest.json").read_text(encoding="utf-8")
    )
    evaluation = EvaluationReport.model_validate_json(
        Path(args.evaluation).read_text(encoding="utf-8")
    )
    gate = release_gate(
        evaluation.metrics,
        min_tool_accuracy=args.min_tool_accuracy,
        min_schema_validity=args.min_schema_validity,
    )
    if not gate["passed"]:
        print(json.dumps(gate, ensure_ascii=False))
        return 2
    if training_manifest.get("tool_schema_sha256") != evaluation.tool_schema_sha256:
        raise ValueError("evaluation schema digest does not match training manifest")
    if not (run_dir / "adapter").is_dir():
        raise FileNotFoundError("training run has no adapter directory")

    destination = Path(args.release_dir)
    if destination.exists():
        raise FileExistsError(f"release directory already exists: {destination}")
    destination.mkdir(parents=True)
    shutil.copytree(run_dir / "adapter", destination / "adapter")
    shutil.copy2(run_dir / "release-manifest.json", destination / "release-manifest.json")
    shutil.copy2(args.evaluation, destination / "evaluation.json")
    shutil.copy2("config/deepresearch_tools.v1.json", destination / "deepresearch_tools.v1.json")
    print(json.dumps({"release_dir": str(destination), "gate": gate}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
