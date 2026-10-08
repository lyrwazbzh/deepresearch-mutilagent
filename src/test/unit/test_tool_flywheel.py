from __future__ import annotations

from src.agent.identity import canonical_json, sha256_hex
from src.agent.schema.model_config import (
    FrozenModelBinding,
    ModelBindingSnapshot,
    ResolvedModelRevision,
)
from src.agent.evaluation.tool_flywheel import (
    FlywheelArtifactStore,
    FlywheelDatasetLoader,
    OfflineToolEvaluator,
    OfflineToolGate,
    ThreadSplitPolicy,
    ToolDecisionPrediction,
    ToolGateConfig,
)
from src.agent.evaluation.model_rollout import ToolCanaryAssigner, ToolCanaryPolicy
from src.agent.evaluation.training import (
    DryRunToolTrainingBackend,
    ToolTrainingConfig,
    ToolTrainingPipeline,
    format_native_tool_sample,
)
from src.agent.backends.workspace import InMemoryObjectStore


def _binding(key: str, model_id: str) -> FrozenModelBinding:
    return FrozenModelBinding(
        binding_key=key,  # type: ignore[arg-type]
        purpose="chat",
        primary=ResolvedModelRevision(model_config_id=model_id, version=1),
    )


def _sample(sample_id: str, agent: str = "deep_scout") -> dict:
    return {
        "sample_id": sample_id,
        "trace_type": "tool",
        "agent": agent,
        "difficulty": "trivial",
        "input": {
            "messages": [{"role": "user", "content": "find sources"}],
            "tools": [
                {
                    "type": "function",
                    "function": {
                        "name": "search_web",
                        "parameters": {
                            "type": "object",
                            "properties": {"query": {"type": "string"}},
                            "required": ["query"],
                            "additionalProperties": False,
                        },
                    },
                }
            ],
        },
        "output": {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "call_0",
                    "type": "function",
                    "function": {
                        "name": "search_web",
                        "arguments": '{"query":"find sources"}',
                    },
                }
            ],
        },
        "label": {
            "acceptable_tools": ["search_web"],
            "argument_assertions": [
                {"kind": "required", "path": "query", "values": None,
                 "minimum": None, "maximum": None, "predicate": None}
            ],
            "label_source": "deterministic_rule",
            "confidence": "high",
            "difficulty": "trivial",
        },
        "diagnostics": {"execution_status": "success"},
    }


def _dataset_store() -> tuple[FlywheelArtifactStore, str]:
    store = FlywheelArtifactStore(InMemoryObjectStore())
    dataset_ref = "dataset/tool/v1"
    train = canonical_json(_sample("sample_1")) + b"\n"
    val = canonical_json(_sample("sample_2")) + b"\n"
    test = b""
    manifest = {
        "dataset_id": "tool",
        "version": "v1",
        "file_hashes": {
            "train.jsonl": sha256_hex(train),
            "val.jsonl": sha256_hex(val),
            "test.jsonl": sha256_hex(test),
        },
    }
    store.publish(
        {
            f"{dataset_ref}/train.jsonl": train,
            f"{dataset_ref}/val.jsonl": val,
            f"{dataset_ref}/test.jsonl": test,
            f"{dataset_ref}/manifest.json": canonical_json(manifest),
        }
    )
    return store, dataset_ref


def test_thread_split_is_stable_and_thread_scoped() -> None:
    policy = ThreadSplitPolicy(seed="stable", train_percent=80, val_percent=10)
    first = policy.split("thread-a")
    assert policy.split("thread-a") == first
    assert all(policy.split("thread-a") == first for _ in range(10))


def test_offline_metrics_separate_execution_and_gate_safety() -> None:
    samples = [_sample("one"), _sample("two", "data_analyst")]
    evaluator = OfflineToolEvaluator()
    incorrect_no_tool = evaluator.evaluate(
        samples,
        lambda _input: ToolDecisionPrediction(selected_tool="NO_TOOL"),
    )
    assert incorrect_no_tool.overall["argument_assertion_pass_rate"] == 0.0

    correct_but_provider_failed = evaluator.evaluate(
        samples,
        lambda _input: ToolDecisionPrediction(
            selected_tool="search_web",
            arguments={"query": "find sources"},
            execution_status="error",
        ),
    )
    assert correct_but_provider_failed.overall["tool_selection_accuracy"] == 1.0
    assert correct_but_provider_failed.overall["execution_success"] == 0.0
    assert set(correct_but_provider_failed.by_agent) == {"deep_scout", "data_analyst"}

    violating = evaluator.evaluate(
        samples,
        lambda _input: ToolDecisionPrediction(
            selected_tool="search_web",
            arguments={"query": "find sources"},
            execution_status="success",
            permission_violation=True,
        ),
    )
    store, dataset_ref = _dataset_store()
    gate = OfflineToolGate(store).compare(
        dataset_ref=dataset_ref,
        baseline=correct_but_provider_failed,
        candidate=violating,
        baseline_config={"model": "base"},
        candidate_config={"model": "candidate"},
        code_revision="rev",
        gate_config=ToolGateConfig(version="v1"),
    )
    assert not gate.passed
    assert "permission_violation_regression" in gate.reasons


def test_training_loader_native_format_artifacts_and_failure_recovery() -> None:
    store, dataset_ref = _dataset_store()
    loader = FlywheelDatasetLoader(store)
    loaded = loader.load_split(dataset_ref, "train")
    formatted = format_native_tool_sample(loaded[0])
    assert formatted["messages"][-1]["tool_calls"][0]["type"] == "function"
    config = ToolTrainingConfig(
        config_version="v1",
        base_model="local/test-model",
        dataset_ref=dataset_ref,
        method="qlora",
    )
    success = ToolTrainingPipeline(
        loader, store, DryRunToolTrainingBackend()
    ).run(config, code_revision="rev")
    assert success.status == "completed"
    assert success.artifact_ref is not None
    assert success.metrics["dry_run"] is True

    failed = ToolTrainingPipeline(
        loader, store, DryRunToolTrainingBackend(fail=True)
    ).run(config, code_revision="rev", resume_checkpoint="checkpoint-1")
    assert failed.status == "failed"
    assert failed.error_type == "RuntimeError"
    assert success.manifest_ref != failed.manifest_ref


def test_canary_assignment_is_deterministic_task_level_and_rollback_safe() -> None:
    baseline = ModelBindingSnapshot(
        bindings={
            "tool": _binding("tool", "baseline"),
            "data": _binding("data", "baseline"),
            "code": _binding("code", "baseline"),
        }
    )
    candidates = {
        key: _binding(key, "candidate") for key in ("tool", "data", "code")
    }
    policy = ToolCanaryPolicy(
        version="canary-v1",
        enabled=True,
        rollout_percent=100,
        assignment_salt="stable",
        eligible_tenants=["tenant"],
        candidate_bindings=candidates,
    )
    assigner = ToolCanaryAssigner(policy)
    first = assigner.assign(
        tenant_id="tenant", assignment_key="task-key", snapshot=baseline
    )
    second = assigner.assign(
        tenant_id="tenant", assignment_key="task-key", snapshot=baseline
    )
    assert first.diagnostics == second.diagnostics
    assert first.snapshot.get("tool").primary.model_config_id == "candidate"
    assert first.snapshot.get("data").primary.model_config_id == "candidate"

    rollback = ToolCanaryAssigner(policy.model_copy(update={"rollback": True}))
    rolled_back = rollback.assign(
        tenant_id="tenant", assignment_key="new-task", snapshot=baseline
    )
    assert rolled_back.snapshot.get("tool").primary.model_config_id == "baseline"
    # The already frozen snapshot does not change when policy rolls back.
    assert first.snapshot.get("tool").primary.model_config_id == "candidate"
