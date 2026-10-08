from __future__ import annotations

import json
import os
import tempfile
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Literal, Protocol
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from src.agent.identity import canonical_json, sha256_hex
from src.agent.backends.persistence.models import DecisionTraceRow
from src.agent.runtime.tool_decisions import (
    NO_TOOL,
    BuiltToolSample,
    ToolDecisionGovernance,
    ToolDecisionLabel,
    ToolDecisionReviewService,
    ToolSampleBuilder,
    argument_assertions_pass,
)


class ArtifactObjectStore(Protocol):
    def put_temp(self, data: bytes, *, prefix: str) -> str: ...
    def promote(self, temp_ref: str, final_ref: str) -> None: ...
    def get(self, storage_ref: str) -> bytes: ...
    def exists(self, storage_ref: str) -> bool: ...
    def delete(self, storage_ref: str) -> None: ...
    def list_prefix(self, prefix: str) -> list[str]: ...


class FilesystemArtifactObjectStore:
    """Local/offline artifact backend with the same immutable publisher contract."""

    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, storage_ref: str) -> Path:
        candidate = (self.root / storage_ref).resolve()
        if candidate != self.root and self.root not in candidate.parents:
            raise ValueError("Artifact path escapes configured root")
        return candidate

    def put_temp(self, data: bytes, *, prefix: str) -> str:
        directory = self._path(prefix)
        directory.mkdir(parents=True, exist_ok=True)
        handle, path = tempfile.mkstemp(prefix="tmp-", dir=directory)
        try:
            with os.fdopen(handle, "wb") as stream:
                stream.write(data)
        except Exception:
            Path(path).unlink(missing_ok=True)
            raise
        return str(Path(path).relative_to(self.root)).replace("\\", "/")

    def promote(self, temp_ref: str, final_ref: str) -> None:
        source = self._path(temp_ref)
        target = self._path(final_ref)
        if target.exists():
            source.unlink(missing_ok=True)
            raise FileExistsError(final_ref)
        target.parent.mkdir(parents=True, exist_ok=True)
        source.replace(target)

    def get(self, storage_ref: str) -> bytes:
        return self._path(storage_ref).read_bytes()

    def exists(self, storage_ref: str) -> bool:
        return self._path(storage_ref).exists()

    def delete(self, storage_ref: str) -> None:
        self._path(storage_ref).unlink(missing_ok=True)

    def list_prefix(self, prefix: str) -> list[str]:
        path = self._path(prefix)
        if path.is_file():
            return [prefix]
        if not path.exists():
            return []
        return sorted(
            str(item.relative_to(self.root)).replace("\\", "/")
            for item in path.rglob("*")
            if item.is_file()
        )


class FlywheelArtifactStore:
    """Immutable logical artifacts over the existing object-store abstraction."""

    def __init__(self, object_store: ArtifactObjectStore) -> None:
        self.object_store = object_store

    def publish(self, files: dict[str, bytes]) -> None:
        collisions = [path for path in files if self.object_store.exists(path)]
        if collisions:
            raise FileExistsError(f"Immutable artifact already exists: {collisions[0]}")
        staged: list[tuple[str, str]] = []
        promoted: list[str] = []
        try:
            for final_ref, payload in sorted(files.items()):
                temp_ref = self.object_store.put_temp(
                    payload, prefix=f"flywheel-staging/{sha256_hex(final_ref)[:16]}"
                )
                staged.append((temp_ref, final_ref))
            # Manifest is promoted last, so its presence is the publication marker.
            staged.sort(key=lambda item: item[1].endswith("manifest.json"))
            for temp_ref, final_ref in staged:
                self.object_store.promote(temp_ref, final_ref)
                promoted.append(final_ref)
        except Exception:
            for temp_ref, _ in staged:
                self.object_store.delete(temp_ref)
            # A snapshot without its manifest is not visible, but leaving partial
            # objects behind would make an otherwise safe retry collide forever.
            for final_ref in promoted:
                self.object_store.delete(final_ref)
            raise

    def put_json_new(self, path: str, payload: dict[str, Any]) -> None:
        self.publish({path: canonical_json(payload)})

    def read_json(self, path: str) -> dict[str, Any]:
        return json.loads(self.object_store.get(path))


@dataclass(frozen=True)
class DatasetSnapshot:
    dataset_ref: str
    manifest: dict[str, Any]


class ThreadSplitPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")
    train_percent: int = Field(default=80, ge=1, le=98)
    val_percent: int = Field(default=10, ge=1, le=98)
    seed: str = Field(default="tool-flywheel-v1", min_length=1)

    def split(self, thread_id: str) -> Literal["train", "val", "test"]:
        if self.train_percent + self.val_percent >= 100:
            raise ValueError("train_percent + val_percent must be below 100")
        bucket = int(sha256_hex(f"{self.seed}:{thread_id}")[:8], 16) % 100
        if bucket < self.train_percent:
            return "train"
        if bucket < self.train_percent + self.val_percent:
            return "val"
        return "test"


class ToolDatasetBuilder:
    BUILDER_VERSION = "tool-dataset-v1"

    def __init__(
        self,
        artifact_store: FlywheelArtifactStore,
        governance: ToolDecisionGovernance,
        reviews: ToolDecisionReviewService,
        *,
        sample_builder: ToolSampleBuilder | None = None,
        split_policy: ThreadSplitPolicy | None = None,
    ) -> None:
        self.artifact_store = artifact_store
        self.governance = governance
        self.reviews = reviews
        self.sample_builder = sample_builder or ToolSampleBuilder()
        self.split_policy = split_policy or ThreadSplitPolicy()

    def build(
        self,
        *,
        dataset_id: str,
        version: str,
        traces: list[DecisionTraceRow],
        git_commit: str,
        created_at: datetime | None = None,
    ) -> DatasetSnapshot:
        if not dataset_id or not version:
            raise ValueError("dataset_id and version are required")
        built: list[BuiltToolSample] = []
        exclusions: Counter[str] = Counter()
        for trace in traces:
            governance_result, governed = self.governance.evaluate(trace)
            if governed is None:
                for reason in governance_result.reasons or ["governance_rejected"]:
                    exclusions[reason] += 1
                continue
            review = self.reviews.latest(trace.decision_id)
            if review is None:
                exclusions["review_missing"] += 1
                continue
            if review.disposition != "accepted" or review.label is None:
                exclusions["review_excluded"] += 1
                continue
            label = ToolDecisionLabel.model_validate(review.label)
            try:
                built.append(self.sample_builder.build(governed, label))
            except ValueError as exc:
                exclusions[f"sample_rejected:{str(exc)}"] += 1
        if not built:
            raise ValueError("No governed and reviewed Tool Decision samples are publishable")
        built.sort(key=lambda item: (item.created_at, item.sample_id))
        split_samples: dict[str, list[BuiltToolSample]] = {
            "train": [],
            "val": [],
            "test": [],
        }
        thread_splits: dict[str, str] = {}
        for sample in built:
            split = thread_splits.setdefault(
                sample.thread_id, self.split_policy.split(sample.thread_id)
            )
            split_samples[split].append(sample)

        base = f"dataset/{dataset_id}/{version}"
        files: dict[str, bytes] = {}
        file_hashes: dict[str, str] = {}
        for split in ("train", "val", "test"):
            content = b"".join(
                canonical_json(item.payload) + b"\n" for item in split_samples[split]
            )
            path = f"{base}/{split}.jsonl"
            files[path] = content
            file_hashes[f"{split}.jsonl"] = sha256_hex(content)

        now = created_at or datetime.now(timezone.utc)
        source_times = [item.created_at for item in built]
        manifest: dict[str, Any] = {
            "dataset_id": dataset_id,
            "version": version,
            "created_at": now.isoformat(),
            "source_time_range": {
                "from": min(source_times).isoformat(),
                "to": max(source_times).isoformat(),
            },
            "sample_count": len(built),
            "split_counts": {
                split: len(items) for split, items in split_samples.items()
            },
            "trace_type_distribution": {"tool": len(built)},
            "agent_distribution": dict(
                sorted(Counter(item.payload["agent"] for item in built).items())
            ),
            "tool_distribution": dict(
                sorted(Counter(_sample_selected_tool(item.payload) for item in built).items())
            ),
            "difficulty_distribution": dict(
                sorted(Counter(item.payload["difficulty"] for item in built).items())
            ),
            "git_commit": git_commit,
            "builder_version": self.BUILDER_VERSION,
            "sample_format_version": ToolSampleBuilder.FORMAT_VERSION,
            "redaction_version": self.governance.redactor.VERSION,
            "split_policy": self.split_policy.model_dump(mode="json"),
            "excluded_count": sum(exclusions.values()),
            "exclusion_reasons": dict(sorted(exclusions.items())),
            "file_hashes": file_hashes,
        }
        files[f"{base}/manifest.json"] = canonical_json(manifest)
        self.artifact_store.publish(files)
        return DatasetSnapshot(dataset_ref=base, manifest=manifest)


def _sample_selected_tool(sample: dict[str, Any]) -> str:
    calls = sample["output"].get("tool_calls") or []
    return calls[0]["function"]["name"] if calls else NO_TOOL


class FlywheelDatasetLoader:
    def __init__(self, artifact_store: FlywheelArtifactStore) -> None:
        self.artifact_store = artifact_store

    def manifest(self, dataset_ref: str) -> dict[str, Any]:
        return self.artifact_store.read_json(f"{dataset_ref}/manifest.json")

    def load_split(self, dataset_ref: str, split: str) -> list[dict[str, Any]]:
        if split not in {"train", "val", "test"}:
            raise ValueError("Unknown dataset split")
        manifest = self.manifest(dataset_ref)
        payload = self.artifact_store.object_store.get(f"{dataset_ref}/{split}.jsonl")
        expected = manifest["file_hashes"][f"{split}.jsonl"]
        if sha256_hex(payload) != expected:
            raise ValueError("Immutable dataset content hash mismatch")
        return [json.loads(line) for line in payload.splitlines() if line.strip()]


class ToolDecisionPrediction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    selected_tool: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    repair_count: int = Field(default=0, ge=0)
    execution_status: Literal["success", "error"] | None = None
    permission_violation: bool = False


class ToolEvalCaseResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    sample_id: str
    agent: str
    tool: str
    difficulty: str
    selection_correct: bool
    arguments_correct: bool
    one_shot_success: bool
    execution_success: bool | None
    permission_violation: bool


class ToolEvalSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")
    overall: dict[str, Any]
    by_agent: dict[str, dict[str, Any]]
    by_tool: dict[str, dict[str, Any]]
    by_difficulty: dict[str, dict[str, Any]]
    cases: list[ToolEvalCaseResult]


class OfflineToolEvaluator:
    def evaluate(
        self,
        samples: list[dict[str, Any]],
        predictor: Callable[[dict[str, Any]], ToolDecisionPrediction],
    ) -> ToolEvalSummary:
        cases: list[ToolEvalCaseResult] = []
        for sample in samples:
            prediction = predictor(sample["input"])
            label = ToolDecisionLabel.model_validate(sample["label"])
            selection_correct = prediction.selected_tool in label.acceptable_tools
            schemas = {
                item["function"]["name"]: item["function"]["parameters"]
                for item in sample["input"]["tools"]
            }
            schema_valid = prediction.selected_tool == NO_TOOL or _json_schema_valid(
                prediction.arguments, schemas.get(prediction.selected_tool)
            )
            assertions_valid = prediction.selected_tool == NO_TOOL or argument_assertions_pass(
                prediction.arguments, label.argument_assertions
            )
            # NO_TOOL has no argument payload to validate. It only passes the
            # argument metric when the reviewed label explicitly accepts NO_TOOL.
            if prediction.selected_tool == NO_TOOL:
                assertions_valid = selection_correct
            arguments_correct = schema_valid and assertions_valid
            cases.append(
                ToolEvalCaseResult(
                    sample_id=sample["sample_id"],
                    agent=sample["agent"],
                    tool=prediction.selected_tool,
                    difficulty=sample["difficulty"],
                    selection_correct=selection_correct,
                    arguments_correct=arguments_correct,
                    one_shot_success=(
                        selection_correct
                        and arguments_correct
                        and prediction.repair_count == 0
                    ),
                    execution_success=(
                        prediction.execution_status == "success"
                        if prediction.execution_status is not None
                        else None
                    ),
                    permission_violation=prediction.permission_violation,
                )
            )
        return ToolEvalSummary(
            overall=_metrics(cases),
            by_agent=_slice_metrics(cases, lambda item: item.agent),
            by_tool=_slice_metrics(cases, lambda item: item.tool),
            by_difficulty=_slice_metrics(cases, lambda item: item.difficulty),
            cases=cases,
        )


def _json_schema_valid(
    value: Any,
    schema: dict[str, Any] | None,
    *,
    root: dict[str, Any] | None = None,
) -> bool:
    if schema is None:
        return False
    root = root or schema
    if "$ref" in schema:
        ref = schema["$ref"]
        if not isinstance(ref, str) or not ref.startswith("#/"):
            return False
        resolved: Any = root
        for part in ref[2:].split("/"):
            if not isinstance(resolved, dict):
                return False
            resolved = resolved.get(part.replace("~1", "/").replace("~0", "~"))
        return isinstance(resolved, dict) and _json_schema_valid(
            value, resolved, root=root
        )
    if "const" in schema and value != schema["const"]:
        return False
    if "allOf" in schema and not all(
        _json_schema_valid(value, child, root=root) for child in schema["allOf"]
    ):
        return False
    if "anyOf" in schema and not any(
        _json_schema_valid(value, child, root=root) for child in schema["anyOf"]
    ):
        return False
    if "oneOf" in schema and sum(
        _json_schema_valid(value, child, root=root) for child in schema["oneOf"]
    ) != 1:
        return False
    if "not" in schema and _json_schema_valid(value, schema["not"], root=root):
        return False
    schema_type = schema.get("type")
    if isinstance(schema_type, list):
        return any(
            _json_schema_valid(value, {**schema, "type": candidate}, root=root)
            for candidate in schema_type
        )
    if schema_type == "object":
        if not isinstance(value, dict):
            return False
        if any(key not in value for key in schema.get("required", [])):
            return False
        if schema.get("additionalProperties") is False:
            if set(value) - set(schema.get("properties", {})):
                return False
        return all(
            _json_schema_valid(value[key], child, root=root)
            for key, child in schema.get("properties", {}).items()
            if key in value
        )
    if "enum" in schema and value not in schema["enum"]:
        return False
    if schema_type == "string":
        if not isinstance(value, str):
            return False
        if len(value) < schema.get("minLength", 0):
            return False
        if "maxLength" in schema and len(value) > schema["maxLength"]:
            return False
    if schema_type == "integer" and (not isinstance(value, int) or isinstance(value, bool)):
        return False
    if schema_type == "number" and (
        not isinstance(value, (int, float)) or isinstance(value, bool)
    ):
        return False
    if schema_type == "boolean" and not isinstance(value, bool):
        return False
    if schema_type == "array":
        if not isinstance(value, list):
            return False
        if len(value) < schema.get("minItems", 0):
            return False
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            return False
        return all(
            _json_schema_valid(item, schema.get("items", {}), root=root)
            for item in value
        )
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            return False
        if "maximum" in schema and value > schema["maximum"]:
            return False
        if "exclusiveMinimum" in schema and value <= schema["exclusiveMinimum"]:
            return False
        if "exclusiveMaximum" in schema and value >= schema["exclusiveMaximum"]:
            return False
    return True


def _metrics(cases: list[ToolEvalCaseResult]) -> dict[str, Any]:
    total = len(cases)
    executed = [item for item in cases if item.execution_success is not None]
    return {
        "case_count": total,
        "tool_selection_accuracy": (
            sum(item.selection_correct for item in cases) / total if total else None
        ),
        "argument_assertion_pass_rate": (
            sum(item.arguments_correct for item in cases) / total if total else None
        ),
        "tool_decision_one_shot_success": (
            sum(item.one_shot_success for item in cases) / total if total else None
        ),
        "execution_success": (
            sum(bool(item.execution_success) for item in executed) / len(executed)
            if executed
            else None
        ),
        "permission_violation_count": sum(item.permission_violation for item in cases),
    }


def _slice_metrics(
    cases: list[ToolEvalCaseResult], key: Callable[[ToolEvalCaseResult], str]
) -> dict[str, dict[str, Any]]:
    grouped: dict[str, list[ToolEvalCaseResult]] = defaultdict(list)
    for case in cases:
        grouped[key(case)].append(case)
    return {name: _metrics(items) for name, items in sorted(grouped.items())}


class ToolGateConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: str = Field(min_length=1)
    selection_delta: float = 0.0
    one_shot_delta: float = 0.0
    critical_tool_max_regression: dict[str, float] = Field(default_factory=dict)
    max_new_permission_violations: int = Field(default=0, ge=0)


class ToolGateResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    evaluation_id: str
    passed: bool
    reasons: list[str]
    dataset_ref: str
    candidate_config: dict[str, Any]
    baseline_config: dict[str, Any]
    code_revision: str
    gate_config: dict[str, Any]
    baseline: dict[str, Any]
    candidate: dict[str, Any]
    created_at: str


class OfflineToolGate:
    def __init__(self, artifact_store: FlywheelArtifactStore) -> None:
        self.artifact_store = artifact_store

    def compare(
        self,
        *,
        dataset_ref: str,
        baseline: ToolEvalSummary,
        candidate: ToolEvalSummary,
        baseline_config: dict[str, Any],
        candidate_config: dict[str, Any],
        code_revision: str,
        gate_config: ToolGateConfig,
        created_at: datetime | None = None,
    ) -> ToolGateResult:
        reasons: list[str] = []
        b = baseline.overall
        c = candidate.overall
        if c["tool_selection_accuracy"] < (
            b["tool_selection_accuracy"] + gate_config.selection_delta
        ):
            reasons.append("tool_selection_below_baseline_delta")
        if c["argument_assertion_pass_rate"] < b["argument_assertion_pass_rate"]:
            reasons.append("argument_assertion_regressed")
        if c["tool_decision_one_shot_success"] < (
            b["tool_decision_one_shot_success"] + gate_config.one_shot_delta
        ):
            reasons.append("one_shot_below_baseline_delta")
        new_permission_violations = (
            c["permission_violation_count"] - b["permission_violation_count"]
        )
        if new_permission_violations > gate_config.max_new_permission_violations:
            reasons.append("permission_violation_regression")
        for tool, max_regression in gate_config.critical_tool_max_regression.items():
            baseline_slice = baseline.by_tool.get(tool)
            candidate_slice = candidate.by_tool.get(tool)
            if baseline_slice is None or candidate_slice is None:
                reasons.append(f"critical_tool_missing:{tool}")
                continue
            regression = (
                baseline_slice["tool_decision_one_shot_success"]
                - candidate_slice["tool_decision_one_shot_success"]
            )
            if regression > max_regression:
                reasons.append(f"critical_tool_regression:{tool}")
        identity = {
            "dataset_ref": dataset_ref,
            "baseline_config": baseline_config,
            "candidate_config": candidate_config,
            "code_revision": code_revision,
            "gate_version": gate_config.version,
        }
        evaluation_id = f"tool_eval_{sha256_hex(canonical_json(identity))[:40]}"
        result = ToolGateResult(
            evaluation_id=evaluation_id,
            passed=not reasons,
            reasons=reasons,
            dataset_ref=dataset_ref,
            candidate_config=candidate_config,
            baseline_config=baseline_config,
            code_revision=code_revision,
            gate_config=gate_config.model_dump(mode="json"),
            baseline=baseline.model_dump(mode="json"),
            candidate=candidate.model_dump(mode="json"),
            created_at=(created_at or datetime.now(timezone.utc)).isoformat(),
        )
        self.artifact_store.put_json_new(
            f"flywheel-evaluations/{evaluation_id}.json",
            result.model_dump(mode="json"),
        )
        return result
