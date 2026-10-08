from __future__ import annotations

import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

from src.agent.identity import canonical_json, sha256_hex
from src.agent.evaluation.tool_flywheel import FlywheelArtifactStore, FlywheelDatasetLoader


class ToolTrainingConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    config_version: str = Field(min_length=1)
    base_model: str = Field(min_length=1)
    dataset_ref: str = Field(pattern=r"^dataset/[^/]+/[^/]+$")
    method: Literal["lora", "qlora"] = "qlora"
    epochs: float = Field(default=1.0, gt=0)
    learning_rate: float = Field(default=2e-4, gt=0)
    batch_size: int = Field(default=1, ge=1)
    gradient_accumulation_steps: int = Field(default=8, ge=1)
    max_sequence_length: int = Field(default=4096, ge=256)
    lora_rank: int = Field(default=16, ge=1)
    lora_alpha: int = Field(default=32, ge=1)
    lora_dropout: float = Field(default=0.05, ge=0, lt=1)
    target_modules: list[str] = Field(
        default_factory=lambda: ["q_proj", "k_proj", "v_proj", "o_proj"]
    )
    random_seed: int = 13


@dataclass(frozen=True)
class TrainingBackendResult:
    artifact_files: dict[str, bytes]
    metrics: dict[str, Any]
    checkpoint_name: str | None = None


class ToolTrainingBackend(Protocol):
    def train(
        self,
        *,
        config: ToolTrainingConfig,
        train_samples: list[dict[str, Any]],
        val_samples: list[dict[str, Any]],
        resume_checkpoint: str | None,
    ) -> TrainingBackendResult: ...


class DryRunToolTrainingBackend:
    """Lightweight contract smoke; it never claims to produce a production model."""

    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail

    def train(
        self,
        *,
        config: ToolTrainingConfig,
        train_samples: list[dict[str, Any]],
        val_samples: list[dict[str, Any]],
        resume_checkpoint: str | None,
    ) -> TrainingBackendResult:
        del resume_checkpoint
        if self.fail:
            raise RuntimeError("intentional dry-run training failure")
        if not train_samples:
            raise ValueError("Training split is empty")
        payload = {
            "kind": "dry_run_adapter",
            "base_model": config.base_model,
            "method": config.method,
            "sample_count": len(train_samples),
        }
        return TrainingBackendResult(
            artifact_files={"adapter/dry-run-adapter.json": canonical_json(payload)},
            metrics={
                "dry_run": True,
                "train_samples": len(train_samples),
                "validation_samples": len(val_samples),
            },
            checkpoint_name="dry-run-checkpoint",
        )


class HuggingFaceToolTrainingBackend:
    """Optional PEFT backend loaded lazily so the API/runtime install stays small."""

    def train(
        self,
        *,
        config: ToolTrainingConfig,
        train_samples: list[dict[str, Any]],
        val_samples: list[dict[str, Any]],
        resume_checkpoint: str | None,
    ) -> TrainingBackendResult:
        try:
            from datasets import Dataset  # type: ignore[import-not-found]
            from peft import LoraConfig, get_peft_model  # type: ignore[import-not-found]
            from transformers import (  # type: ignore[import-not-found]
                AutoModelForCausalLM,
                AutoTokenizer,
                BitsAndBytesConfig,
                DataCollatorForLanguageModeling,
                Trainer,
                TrainingArguments,
            )
        except ImportError as exc:  # pragma: no cover - optional GPU stack
            raise RuntimeError(
                "QLoRA/SFT requires transformers, datasets, peft, accelerate and "
                "bitsandbytes for qlora"
            ) from exc

        tokenizer = AutoTokenizer.from_pretrained(config.base_model)
        if tokenizer.pad_token is None:
            tokenizer.pad_token = tokenizer.eos_token
        quantization = None
        if config.method == "qlora":
            quantization = BitsAndBytesConfig(
                load_in_4bit=True,
                bnb_4bit_quant_type="nf4",
                bnb_4bit_use_double_quant=True,
            )
        model = AutoModelForCausalLM.from_pretrained(
            config.base_model,
            quantization_config=quantization,
            device_map="auto" if quantization is not None else None,
        )
        model = get_peft_model(
            model,
            LoraConfig(
                r=config.lora_rank,
                lora_alpha=config.lora_alpha,
                lora_dropout=config.lora_dropout,
                target_modules=config.target_modules,
                task_type="CAUSAL_LM",
            ),
        )

        def tokenize(sample: dict[str, Any]) -> dict[str, Any]:
            formatted = format_native_tool_sample(sample)
            text = tokenizer.apply_chat_template(
                formatted["messages"],
                tools=formatted["tools"],
                tokenize=False,
                add_generation_prompt=False,
            )
            return tokenizer(
                text,
                truncation=True,
                max_length=config.max_sequence_length,
            )

        train_dataset = Dataset.from_list(train_samples).map(tokenize)
        eval_dataset = Dataset.from_list(val_samples).map(tokenize) if val_samples else None
        with tempfile.TemporaryDirectory(prefix="tool-training-") as output_dir:
            arguments = TrainingArguments(
                output_dir=output_dir,
                num_train_epochs=config.epochs,
                learning_rate=config.learning_rate,
                per_device_train_batch_size=config.batch_size,
                gradient_accumulation_steps=config.gradient_accumulation_steps,
                seed=config.random_seed,
                save_strategy="epoch",
                eval_strategy="epoch" if eval_dataset is not None else "no",
                report_to=[],
            )
            trainer = Trainer(
                model=model,
                args=arguments,
                train_dataset=train_dataset,
                eval_dataset=eval_dataset,
                data_collator=DataCollatorForLanguageModeling(tokenizer, mlm=False),
            )
            result = trainer.train(resume_from_checkpoint=resume_checkpoint)
            adapter_dir = Path(output_dir) / "adapter"
            model.save_pretrained(adapter_dir)
            tokenizer.save_pretrained(adapter_dir)
            files = {
                str(path.relative_to(output_dir)).replace("\\", "/"): path.read_bytes()
                for path in adapter_dir.rglob("*")
                if path.is_file()
            }
            return TrainingBackendResult(
                artifact_files=files,
                metrics=dict(result.metrics),
                checkpoint_name=str(result.metrics.get("epoch", "completed")),
            )


def format_native_tool_sample(sample: dict[str, Any]) -> dict[str, Any]:
    """Append the native assistant Tool Call/NO_TOOL output to its production input."""

    return {
        "messages": [*sample["input"]["messages"], sample["output"]],
        "tools": sample["input"]["tools"],
    }


class TrainingRunResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    training_run_id: str
    status: Literal["completed", "failed"]
    artifact_ref: str | None = None
    manifest_ref: str
    metrics: dict[str, Any] = Field(default_factory=dict)
    error_type: str | None = None


class ToolTrainingPipeline:
    def __init__(
        self,
        loader: FlywheelDatasetLoader,
        artifact_store: FlywheelArtifactStore,
        backend: ToolTrainingBackend,
    ) -> None:
        self.loader = loader
        self.artifact_store = artifact_store
        self.backend = backend

    def run(
        self,
        config: ToolTrainingConfig,
        *,
        code_revision: str,
        resume_checkpoint: str | None = None,
    ) -> TrainingRunResult:
        dataset_manifest = self.loader.manifest(config.dataset_ref)
        identity = {
            "dataset_ref": config.dataset_ref,
            "training_config": config.model_dump(mode="json"),
            "code_revision": code_revision,
        }
        training_run_id = f"train_{sha256_hex(canonical_json(identity))[:40]}"
        attempt = 1 + len(
            self.artifact_store.object_store.list_prefix(
                f"training-runs/{training_run_id}/attempts/"
            )
        )
        manifest_ref = (
            f"training-runs/{training_run_id}/attempts/{attempt:04d}/manifest.json"
        )
        try:
            train_samples = self.loader.load_split(config.dataset_ref, "train")
            val_samples = self.loader.load_split(config.dataset_ref, "val")
            backend_result = self.backend.train(
                config=config,
                train_samples=train_samples,
                val_samples=val_samples,
                resume_checkpoint=resume_checkpoint,
            )
            artifact_ref = f"training-runs/{training_run_id}/artifacts/{attempt:04d}"
            files = {
                f"{artifact_ref}/{name}": payload
                for name, payload in backend_result.artifact_files.items()
            }
            file_hashes = {
                name: sha256_hex(payload)
                for name, payload in backend_result.artifact_files.items()
            }
            manifest = {
                "training_run_id": training_run_id,
                "status": "completed",
                "base_model": config.base_model,
                "dataset_id": dataset_manifest["dataset_id"],
                "dataset_version": dataset_manifest["version"],
                "dataset_ref": config.dataset_ref,
                "training_config": config.model_dump(mode="json"),
                "code_revision": code_revision,
                "artifact_ref": artifact_ref,
                "artifact_hashes": file_hashes,
                "checkpoint": backend_result.checkpoint_name,
                "metrics": backend_result.metrics,
            }
            files[manifest_ref] = canonical_json(manifest)
            self.artifact_store.publish(files)
            return TrainingRunResult(
                training_run_id=training_run_id,
                status="completed",
                artifact_ref=artifact_ref,
                manifest_ref=manifest_ref,
                metrics=backend_result.metrics,
            )
        except Exception as exc:
            failure = {
                "training_run_id": training_run_id,
                "status": "failed",
                "dataset_ref": config.dataset_ref,
                "base_model": config.base_model,
                "training_config": config.model_dump(mode="json"),
                "code_revision": code_revision,
                "resume_checkpoint": resume_checkpoint,
                "error_type": type(exc).__name__,
            }
            self.artifact_store.put_json_new(manifest_ref, failure)
            return TrainingRunResult(
                training_run_id=training_run_id,
                status="failed",
                manifest_ref=manifest_ref,
                error_type=type(exc).__name__,
            )
