from __future__ import annotations

import argparse
import json
from pathlib import Path

from src.agent.evaluation.tool_flywheel import (
    FilesystemArtifactObjectStore,
    FlywheelArtifactStore,
    FlywheelDatasetLoader,
)
from src.agent.evaluation.training import (
    DryRunToolTrainingBackend,
    HuggingFaceToolTrainingBackend,
    ToolTrainingConfig,
    ToolTrainingPipeline,
)


def main() -> None:
    parser = argparse.ArgumentParser(description="Train a Tool Decision LoRA/QLoRA adapter")
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--artifact-root", required=True, type=Path)
    parser.add_argument("--code-revision", required=True)
    parser.add_argument("--resume-checkpoint")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    config = ToolTrainingConfig.model_validate_json(args.config.read_text("utf-8"))
    store = FlywheelArtifactStore(FilesystemArtifactObjectStore(args.artifact_root))
    backend = (
        DryRunToolTrainingBackend() if args.dry_run else HuggingFaceToolTrainingBackend()
    )
    result = ToolTrainingPipeline(
        FlywheelDatasetLoader(store), store, backend
    ).run(
        config,
        code_revision=args.code_revision,
        resume_checkpoint=args.resume_checkpoint,
    )
    print(json.dumps(result.model_dump(mode="json"), ensure_ascii=False, sort_keys=True))
    if result.status != "completed":
        raise SystemExit(1)
