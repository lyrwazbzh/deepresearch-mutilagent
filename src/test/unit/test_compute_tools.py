from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from src.agent.tools.compute import (
    BundleOutput,
    GenerateChartAdapter,
    PublishedBundle,
    ResolvedResource,
    SandboxExecuteAdapter,
    SandboxLimits,
    SandboxRunResult,
    StructuredData,
    _collect_outputs,
    _render_table,
)
from src.agent.identity import canonical_json, content_hash, sha256_hex
from src.agent.schema.state import AnalysisRequest, Evidence, ResearchState
from src.agent.runtime.tool_runtime import ToolExecutionContext
from src.agent.tools.compute import RenderTableArgs


class NeverCancelled:
    def is_cancelled(self) -> bool:
        return False


def context() -> ToolExecutionContext:
    return ToolExecutionContext(
        tenant_id="tenant",
        user_id="user",
        task_id="task",
        thread_id="thread",
        node_run_id="node",
        cancellation_token=NeverCancelled(),
        deadline=datetime.now(timezone.utc) + timedelta(minutes=5),
        inline_budget=32_000,
        operation_key="a" * 64,
        resource_scope={
            "analysis_id": "analysis_1",
            "allowed_evidence_ids": ["e1"],
            "allowed_dataset_refs": [],
        },
    )


def evidence() -> Evidence:
    data = {"columns": ["x", "y"], "rows": [["A", 1], ["B", 2]]}
    return Evidence(
        evidence_id="e1",
        hypothesis_id="h1",
        summary="data",
        excerpt="rows",
        source_type="sql",
        stance="support",
        relevance=1,
        data=data,
        content_hash=content_hash(data),
    )


class FakeResolver:
    def __init__(self) -> None:
        self.item = evidence()
        self.request = AnalysisRequest(
            analysis_id="analysis_1",
            hypothesis_id="h1",
            objective="compute",
            input_evidence_ids=["e1"],
            expected_output="artifact",
        )
        self.state = ResearchState(
            tenant_id="tenant",
            user_id="user",
            thread_id="thread",
            task_id="task",
            query="question",
            evidence_pool=[self.item],
        )

    def analysis_request(self, tool_context, analysis_id):
        assert tool_context.resource_scope["analysis_id"] == analysis_id
        return self.state, self.request

    def evidence(self, tool_context, analysis_id, evidence_id, name):
        assert analysis_id == "analysis_1"
        assert evidence_id == "e1"
        return ResolvedResource(
            name=name,
            data=canonical_json(self.item.data),
            content_hash=self.item.content_hash,
            evidence_id=evidence_id,
            media_type="application/json",
        )

    def dataset(self, tool_context, dataset_ref, name):
        raise AssertionError((tool_context, dataset_ref, name))

    def validate_evidence_ids(self, tool_context, evidence_ids):
        assert tool_context.resource_scope["analysis_id"] == "analysis_1"
        assert evidence_ids == ["e1"]
        return "analysis_1", [self.item]


class CapturingPublisher:
    def __init__(self) -> None:
        self.calls = []

    def publish(self, tool_context, **kwargs):
        self.calls.append((tool_context, kwargs))
        outputs = [
            BundleOutput(
                workspace_path=f"compute/call/outputs/{name}",
                content_hash=sha256_hex(payload),
                media_type=media_type,
                size=len(payload),
            )
            for name, payload, media_type in kwargs["outputs"]
        ]
        return PublishedBundle("call", "compute/call/manifest.json", outputs)


class WritingRunner:
    def __init__(self) -> None:
        self.spec = None

    def run(self, spec):
        self.spec = spec
        manifest = json.loads((spec.inputs_dir / "_manifest.json").read_text("utf-8"))
        assert manifest["inputs"][0]["path"] == "/inputs/input.json"
        assert (spec.inputs_dir / "input.json").read_bytes()
        assert 299_000 <= spec.timeout_ms <= 300_000
        (spec.outputs_dir / "answer.json").write_text('{"answer": 42}', "utf-8")
        return SandboxRunResult(exit_code=0, runtime_ms=12, peak_memory_bytes=1024)


def test_sandbox_adapter_uses_runner_and_publishes_controlled_bundle() -> None:
    resolver = FakeResolver()
    publisher = CapturingPublisher()
    runner = WritingRunner()
    adapter = SandboxExecuteAdapter(resolver, publisher, runner)

    result = adapter.execute(
        context(),
        {
            "analysis_id": "analysis_1",
            "code": "raise AssertionError('must only run in sandbox')",
            "inputs": [{"name": "input.json", "evidence_id": "e1"}],
            "timeout_ms": 300_000,
        },
    )

    assert result.ok
    assert result.references[0].workspace_path.endswith("/outputs/answer.json")
    assert result.data["evidence_ids"] == ["e1"]
    assert result.data["lineage_manifest_path"] == "compute/call/manifest.json"
    publish = publisher.calls[0][1]
    assert publish["analysis_id"] == "analysis_1"
    assert publish["code"].startswith(b"raise AssertionError")
    assert publish["evidence"] == [resolver.item]


def test_sandbox_output_hard_limits_are_enforced(tmp_path) -> None:
    output = tmp_path / "outputs"
    output.mkdir()
    (output / "large.bin").write_bytes(b"12345")
    with pytest.raises(ValueError, match="single-file limit"):
        _collect_outputs(
            output,
            SandboxLimits(single_output_bytes=4, total_output_bytes=10),
        )


def test_generate_chart_is_deterministic_and_lineaged() -> None:
    resolver = FakeResolver()
    first = CapturingPublisher()
    second = CapturingPublisher()
    arguments = {
        "chart_type": "bar",
        "data": {"columns": ["x", "y"], "rows": [["A", 1], ["B", 2]]},
        "evidence_ids": ["e1"],
        "x": "x",
        "y": "y",
        "title": "Chart",
    }

    first_result = GenerateChartAdapter(resolver, first).execute(context(), arguments)
    second_result = GenerateChartAdapter(resolver, second).execute(context(), arguments)

    first_png = first.calls[0][1]["outputs"][0][1]
    second_png = second.calls[0][1]["outputs"][0][1]
    assert first_result.ok and second_result.ok
    assert first_png.startswith(b"\x89PNG")
    assert first_png == second_png
    assert first_result.data["evidence_ids"] == ["e1"]


def test_chart_and_table_limits_reject_oversized_payloads() -> None:
    resolver = FakeResolver()
    with pytest.raises(ValueError, match="20,000 point"):
        GenerateChartAdapter(resolver, CapturingPublisher()).execute(
            context(),
            {
                "chart_type": "line",
                "data": {
                    "columns": ["x", "y"],
                    "rows": [[index, index] for index in range(20_001)],
                },
                "evidence_ids": ["e1"],
                "x": "x",
                "y": "y",
            },
        )

    args = RenderTableArgs(
        inline=StructuredData(
            columns=["value"],
            rows=[["a|b\nline"], ["x"], ["y"]],
        ),
        evidence_ids=["e1"],
        format="markdown",
        max_rows=2,
    )
    payload, row_count, truncated = _render_table(args, args.inline)
    assert row_count == 2
    assert truncated
    assert b"[TRUNCATED]" in payload
    assert b"a\\|b<br>line" in payload
