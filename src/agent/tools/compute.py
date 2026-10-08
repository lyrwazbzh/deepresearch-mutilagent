from __future__ import annotations

import csv
import html
import io
import json
import mimetypes
import os
import re
import shutil
import stat
import subprocess
import tarfile
import tempfile
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import select

from src.agent.tools.datasets import DatasetService
from src.agent.tools.documents import DocumentService
from src.agent.config import DEFAULT_PHASE6_LIMITS, Phase6Limits
from src.agent.identity import canonical_json, sha256_hex
from src.agent.schema.state import AnalysisRequest, Evidence, ResearchState
from src.agent.backends.persistence.database import SessionFactory
from src.agent.backends.persistence.models import ResearchTaskRow
from src.agent.runtime.tool_runtime import (
    ToolDefinition,
    ToolError,
    ToolErrorType,
    ToolExecutionContext,
    ToolReference,
    ToolResult,
)
from src.agent.backends.workspace import WorkspaceService, normalize_workspace_path


@dataclass(frozen=True)
class ResolvedResource:
    name: str
    data: bytes
    content_hash: str
    evidence_id: str | None = None
    dataset_ref: str | None = None
    media_type: str = "application/octet-stream"


class ComputeResourceResolver:
    def __init__(
        self,
        session_factory: SessionFactory,
        workspace: WorkspaceService,
        documents: DocumentService,
        datasets: DatasetService,
    ) -> None:
        self.session_factory = session_factory
        self.workspace = workspace
        self.documents = documents
        self.datasets = datasets

    def state(self, context: ToolExecutionContext) -> ResearchState:
        with self.session_factory() as session:
            task = session.scalar(
                select(ResearchTaskRow).where(
                    ResearchTaskRow.tenant_id == context.tenant_id,
                    ResearchTaskRow.task_id == context.task_id,
                )
            )
            if task is None:
                raise FileNotFoundError("ResearchTask was not found")
            return ResearchState.model_validate(task.latest_state)

    def analysis_request(
        self, context: ToolExecutionContext, analysis_id: str
    ) -> tuple[ResearchState, AnalysisRequest]:
        state = self.state(context)
        request = next(
            (item for item in state.analysis_requests if item.analysis_id == analysis_id), None
        )
        if request is None:
            raise FileNotFoundError("AnalysisRequest was not found")
        if request.status != "pending":
            raise PermissionError("AnalysisRequest is not pending")
        scoped = context.resource_scope.get("analysis_id")
        if scoped is not None and scoped != analysis_id:
            raise PermissionError("Tool call is outside the current AnalysisRequest")
        return state, request

    def evidence(
        self,
        context: ToolExecutionContext,
        analysis_id: str,
        evidence_id: str,
        name: str,
    ) -> ResolvedResource:
        state, request = self.analysis_request(context, analysis_id)
        if evidence_id not in request.input_evidence_ids:
            raise PermissionError("Evidence is outside the AnalysisRequest")
        allowed = set(context.resource_scope.get("allowed_evidence_ids", []))
        if allowed and evidence_id not in allowed:
            raise PermissionError("Evidence is outside the node resource scope")
        evidence = next(
            (item for item in state.evidence_pool if item.evidence_id == evidence_id), None
        )
        if evidence is None:
            raise FileNotFoundError("Evidence was not found")
        if evidence.data is not None:
            payload = canonical_json(evidence.data)
            media_type = "application/json"
        elif evidence.workspace_path is not None:
            payload = self.workspace.read(
                context.tenant_id, context.task_id, evidence.workspace_path
            )
            media_type = self.workspace.metadata(
                context.tenant_id, context.task_id, evidence.workspace_path
            ).mime_type
        else:
            assert evidence.doc_id and evidence.chunk_id
            chunk = self.documents.read_chunk(
                context.tenant_id, evidence.doc_id, evidence.chunk_id
            )
            payload = chunk.text.encode("utf-8")
            media_type = "text/plain; charset=utf-8"
        if sha256_hex(payload) != evidence.content_hash:
            raise RuntimeError("Evidence content integrity check failed")
        return ResolvedResource(
            name=_safe_resource_name(name),
            data=payload,
            content_hash=evidence.content_hash,
            evidence_id=evidence_id,
            media_type=media_type,
        )

    def dataset(
        self,
        context: ToolExecutionContext,
        dataset_ref: str,
        name: str,
    ) -> ResolvedResource:
        allowed = set(context.resource_scope.get("allowed_dataset_refs", []))
        if allowed and dataset_ref not in allowed:
            raise PermissionError("Dataset is outside the node resource scope")
        row = self.datasets.resolve(context.tenant_id, context.task_id, dataset_ref)
        if row.status != "ready":
            raise RuntimeError("dataset_not_ready")
        payload = self.workspace.read(
            context.tenant_id, context.task_id, row.source_workspace_path
        )
        if sha256_hex(payload) != row.source_content_hash:
            raise RuntimeError("Dataset content integrity check failed")
        media_type = self.workspace.metadata(
            context.tenant_id, context.task_id, row.source_workspace_path
        ).mime_type
        return ResolvedResource(
            name=_safe_resource_name(name),
            data=payload,
            content_hash=row.source_content_hash,
            dataset_ref=dataset_ref,
            media_type=media_type,
        )

    def validate_evidence_ids(
        self,
        context: ToolExecutionContext,
        evidence_ids: list[str],
    ) -> tuple[str, list[Evidence]]:
        analysis_id = str(context.resource_scope.get("analysis_id") or "")
        if not analysis_id:
            raise PermissionError("Compute tool requires an AnalysisRequest scope")
        state, request = self.analysis_request(context, analysis_id)
        if not evidence_ids or not set(evidence_ids) <= set(request.input_evidence_ids):
            raise PermissionError("Compute lineage is outside the AnalysisRequest")
        by_id = {item.evidence_id: item for item in state.evidence_pool}
        if not set(evidence_ids) <= set(by_id):
            raise FileNotFoundError("Compute lineage references unknown Evidence")
        return analysis_id, [by_id[item] for item in evidence_ids]

    def structured_data(
        self,
        context: ToolExecutionContext,
        data_ref: "DataRef",
        evidence_ids: list[str],
    ) -> "StructuredData":
        _, evidence = self.validate_evidence_ids(context, evidence_ids)
        if data_ref.workspace_path is not None:
            path = normalize_workspace_path(data_ref.workspace_path)
            if not any(item.workspace_path == path for item in evidence):
                raise PermissionError("Workspace data_ref is not in Evidence lineage")
            payload = self.workspace.read(context.tenant_id, context.task_id, path)
            return _parse_structured(payload, path)
        assert data_ref.doc_id and data_ref.chunk_id
        if not any(
            item.doc_id == data_ref.doc_id and item.chunk_id == data_ref.chunk_id
            for item in evidence
        ):
            raise PermissionError("Document data_ref is not in Evidence lineage")
        chunk = self.documents.read_chunk(
            context.tenant_id, data_ref.doc_id, data_ref.chunk_id
        )
        return _parse_structured(chunk.text.encode("utf-8"), "data.json")


@dataclass(frozen=True)
class SandboxLimits:
    cpus: float = DEFAULT_PHASE6_LIMITS.sandbox_cpus
    memory_bytes: int = DEFAULT_PHASE6_LIMITS.sandbox_memory_bytes
    writable_disk_bytes: int = DEFAULT_PHASE6_LIMITS.sandbox_writable_disk_bytes
    pids: int = DEFAULT_PHASE6_LIMITS.sandbox_pids
    log_bytes: int = DEFAULT_PHASE6_LIMITS.sandbox_log_bytes
    single_output_bytes: int = DEFAULT_PHASE6_LIMITS.sandbox_single_output_bytes
    total_output_bytes: int = DEFAULT_PHASE6_LIMITS.sandbox_total_output_bytes
    max_timeout_ms: int = DEFAULT_PHASE6_LIMITS.sandbox_max_timeout_ms

    @classmethod
    def from_phase6(cls, limits: Phase6Limits) -> "SandboxLimits":
        return cls(
            cpus=limits.sandbox_cpus,
            memory_bytes=limits.sandbox_memory_bytes,
            writable_disk_bytes=limits.sandbox_writable_disk_bytes,
            pids=limits.sandbox_pids,
            log_bytes=limits.sandbox_log_bytes,
            single_output_bytes=limits.sandbox_single_output_bytes,
            total_output_bytes=limits.sandbox_total_output_bytes,
            max_timeout_ms=limits.sandbox_max_timeout_ms,
        )


@dataclass(frozen=True)
class SandboxSpec:
    tool_call_id: str
    code_path: Path
    inputs_dir: Path
    outputs_dir: Path
    timeout_ms: int
    cancellation_token: Any


@dataclass(frozen=True)
class SandboxRunResult:
    exit_code: int
    stdout: str = ""
    stderr: str = ""
    truncated_output: bool = False
    runtime_ms: int = 0
    peak_memory_bytes: int | None = None
    limit_reason: str | None = None


class SandboxRunner(Protocol):
    def run(self, spec: SandboxSpec) -> SandboxRunResult: ...


class DockerSandboxRunner:
    """Runs untrusted code only inside a locked-down Docker container."""

    def __init__(
        self,
        *,
        image: str = "python:3.12.7-slim-bookworm",
        runtime: str = "runsc",
        limits: SandboxLimits | None = None,
    ) -> None:
        self.image = image
        self.runtime = runtime
        self.limits = limits or SandboxLimits()

    @property
    def runtime_descriptor(self) -> str:
        return f"{self.image}/docker-{self.runtime}-network-none"

    def run(self, spec: SandboxSpec) -> SandboxRunResult:
        docker = shutil.which("docker")
        if docker is None:
            raise RuntimeError("Docker container runtime is unavailable")
        name = f"deepresearch-{sha256_hex(spec.tool_call_id)[:20]}"
        command = [
            docker,
            "run",
            "--name",
            name,
            "--runtime",
            self.runtime,
            "--network",
            "none",
            "--cpus",
            str(self.limits.cpus),
            "--memory",
            str(self.limits.memory_bytes),
            "--memory-swap",
            str(self.limits.memory_bytes),
            "--pids-limit",
            str(self.limits.pids),
            "--read-only",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--user",
            "65532:65532",
            "--tmpfs",
            (
                "/tmp:rw,noexec,nosuid,nodev,size="
                f"{self.limits.writable_disk_bytes - self.limits.total_output_bytes}"
            ),
            "--tmpfs",
            f"/outputs:rw,noexec,nosuid,nodev,size={self.limits.total_output_bytes}",
            "--mount",
            f"type=bind,src={spec.inputs_dir.resolve()},dst=/inputs,readonly",
            "--mount",
            f"type=bind,src={spec.code_path.resolve()},dst=/work/code.py,readonly",
            "-e",
            "DEEPRESEARCH_INPUT_MANIFEST=/inputs/_manifest.json",
            "-e",
            "DEEPRESEARCH_OUTPUT_DIR=/outputs",
            "-w",
            "/work",
            self.image,
            "sh",
            "-c",
            (
                "python -I /work/code.py; status=$?; "
                "printf '%s' \"$status\" > /tmp/deepresearch-exit-code; "
                "while :; do sleep 3600; done"
            ),
        ]
        started = time.monotonic()
        process = subprocess.Popen(  # noqa: S603 - command is a fixed Docker argv
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env={"PATH": os.environ.get("PATH", "")},
        )
        stdout = bytearray()
        stderr = bytearray()
        lock = threading.Lock()
        truncated = {"value": False}

        def drain(stream: Any, target: bytearray) -> None:
            while True:
                chunk = stream.read(65_536)
                if not chunk:
                    return
                with lock:
                    remaining = self.limits.log_bytes - len(stdout) - len(stderr)
                    if remaining > 0:
                        target.extend(chunk[:remaining])
                    if len(chunk) > max(0, remaining):
                        truncated["value"] = True

        threads = [
            threading.Thread(target=drain, args=(process.stdout, stdout), daemon=True),
            threading.Thread(target=drain, args=(process.stderr, stderr), daemon=True),
        ]
        for thread in threads:
            thread.start()
        timed_out = False
        cancelled = False
        script_exit_code: int | None = None
        try:
            while process.poll() is None and script_exit_code is None:
                if spec.cancellation_token.is_cancelled():
                    cancelled = True
                    break
                if (time.monotonic() - started) * 1000 >= spec.timeout_ms:
                    timed_out = True
                    break
                marker = subprocess.run(  # noqa: S603 - fixed Docker lifecycle command
                    [docker, "exec", name, "cat", "/tmp/deepresearch-exit-code"],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    check=False,
                    text=True,
                    env={"PATH": os.environ.get("PATH", "")},
                )
                if marker.returncode == 0:
                    try:
                        script_exit_code = int(marker.stdout.strip())
                    except (AttributeError, ValueError) as exc:
                        raise RuntimeError(
                            "Sandbox returned an invalid process status"
                        ) from exc
                    break
                time.sleep(0.05)
            if timed_out or cancelled:
                raise (
                    InterruptedError("Sandbox execution was cancelled")
                    if cancelled
                    else TimeoutError("Sandbox execution timed out")
                )
            if script_exit_code is None:
                raise RuntimeError("Sandbox container exited before reporting status")
            archive = subprocess.Popen(  # noqa: S603 - fixed Docker lifecycle command
                [docker, "exec", name, "tar", "-C", "/outputs", "-cf", "-", "."],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env={"PATH": os.environ.get("PATH", "")},
            )
            if archive.stdout is None or archive.stderr is None:
                raise RuntimeError("Sandbox output archive pipes are unavailable")
            try:
                with tarfile.open(fileobj=archive.stdout, mode="r|*") as bundle:
                    for member in bundle:
                        if not (member.isfile() or member.isdir()):
                            raise RuntimeError(
                                "Sandbox outputs may contain only files and directories"
                            )
                        bundle.extract(
                            member,
                            path=spec.outputs_dir.resolve(),
                            filter="data",
                        )
                archive_error = archive.stderr.read(self.limits.log_bytes)
                archive_code = archive.wait(timeout=10)
            except Exception:
                archive.kill()
                archive.wait(timeout=10)
                raise
            if archive_code != 0:
                detail = archive_error.decode("utf-8", errors="replace").strip()
                raise RuntimeError(
                    "Sandbox outputs could not be collected"
                    + (f": {detail[:256]}" if detail else "")
                )
        finally:
            subprocess.run(  # noqa: S603 - fixed Docker lifecycle command
                [docker, "kill", name],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
                env={"PATH": os.environ.get("PATH", "")},
            )
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=10)
            for thread in threads:
                thread.join(timeout=2)
            subprocess.run(  # noqa: S603 - fixed Docker lifecycle command
                [docker, "rm", "--force", name],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
                env={"PATH": os.environ.get("PATH", "")},
            )
        assert script_exit_code is not None
        decoded_stderr = stderr.decode("utf-8", errors="replace")
        disk_exceeded = "no space left on device" in decoded_stderr.lower()
        return SandboxRunResult(
            exit_code=script_exit_code,
            stdout=stdout.decode("utf-8", errors="replace"),
            stderr=decoded_stderr,
            truncated_output=truncated["value"],
            runtime_ms=max(0, int((time.monotonic() - started) * 1000)),
            limit_reason=(
                "disk"
                if disk_exceeded
                else "memory" if script_exit_code == 137 else None
            ),
        )


@dataclass(frozen=True)
class BundleOutput:
    workspace_path: str
    content_hash: str
    media_type: str
    size: int


@dataclass(frozen=True)
class PublishedBundle:
    tool_call_id: str
    manifest_path: str
    outputs: list[BundleOutput]


class ComputeBundlePublisher:
    def __init__(self, workspace: WorkspaceService) -> None:
        self.workspace = workspace

    def publish(
        self,
        context: ToolExecutionContext,
        *,
        analysis_id: str,
        evidence: list[Evidence],
        datasets: list[ResolvedResource],
        runtime: str,
        outputs: list[tuple[str, bytes, str]],
        code: bytes | None = None,
        extra_manifest: dict[str, Any] | None = None,
    ) -> PublishedBundle:
        tool_call_id = f"compute_{context.operation_key[:40]}"
        root = f"compute/{tool_call_id}"
        if code is not None:
            self.workspace.publish_bytes(
                tenant_id=context.tenant_id,
                task_id=context.task_id,
                path=f"{root}/code.py",
                data=code,
                mime_type="text/x-python; charset=utf-8",
                operation_key=sha256_hex(f"{context.operation_key}|code.py"),
                producer_tool_call_id=tool_call_id,
            )
        published: list[BundleOutput] = []
        for relative_path, payload, media_type in outputs:
            safe = _safe_output_path(relative_path)
            file = self.workspace.publish_bytes(
                tenant_id=context.tenant_id,
                task_id=context.task_id,
                path=f"{root}/outputs/{safe}",
                data=payload,
                mime_type=media_type,
                operation_key=sha256_hex(
                    f"{context.operation_key}|output:{safe}"
                ),
                producer_tool_call_id=tool_call_id,
            )
            published.append(
                BundleOutput(
                    workspace_path=file.workspace_path,
                    content_hash=file.content_hash,
                    media_type=file.mime_type,
                    size=file.size,
                )
            )
        manifest: dict[str, Any] = {
            "tool_call_id": tool_call_id,
            "analysis_id": analysis_id,
            "input_evidence": [
                {"evidence_id": item.evidence_id, "content_hash": item.content_hash}
                for item in evidence
            ],
            "input_datasets": [
                {"dataset_ref": item.dataset_ref, "content_hash": item.content_hash}
                for item in datasets
                if item.dataset_ref
            ],
            "runtime": runtime,
            "outputs": [
                {
                    "workspace_path": item.workspace_path,
                    "content_hash": item.content_hash,
                }
                for item in published
            ],
        }
        if code is not None:
            manifest["code_hash"] = sha256_hex(code)
        if extra_manifest:
            manifest.update(extra_manifest)
        manifest_path = f"{root}/manifest.json"
        self.workspace.publish_bytes(
            tenant_id=context.tenant_id,
            task_id=context.task_id,
            path=manifest_path,
            data=canonical_json(manifest),
            mime_type="application/json",
            operation_key=sha256_hex(f"{context.operation_key}|manifest.json"),
            producer_tool_call_id=tool_call_id,
        )
        return PublishedBundle(tool_call_id, manifest_path, published)


class SandboxInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=128)
    evidence_id: str | None = None
    dataset_ref: str | None = None

    @model_validator(mode="after")
    def one_reference(self) -> "SandboxInput":
        if (self.evidence_id is None) == (self.dataset_ref is None):
            raise ValueError("input requires exactly one evidence_id or dataset_ref")
        _safe_resource_name(self.name)
        return self


class SandboxExecuteArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    analysis_id: str = Field(min_length=1)
    code: str = Field(min_length=1)
    inputs: list[SandboxInput] = Field(default_factory=list)
    timeout_ms: int = Field(
        default=DEFAULT_PHASE6_LIMITS.sandbox_default_timeout_ms,
        gt=0,
        le=DEFAULT_PHASE6_LIMITS.sandbox_max_timeout_ms,
    )

    @model_validator(mode="after")
    def names_are_unique(self) -> "SandboxExecuteArgs":
        names = [item.name for item in self.inputs]
        if len(names) != len(set(names)):
            raise ValueError("Sandbox input names must be unique")
        if "_manifest.json" in names:
            raise ValueError("_manifest.json is reserved")
        if not any(item.evidence_id for item in self.inputs):
            raise ValueError("Sandbox execution requires at least one Evidence input")
        return self


class SandboxExecuteAdapter:
    def __init__(
        self,
        resolver: ComputeResourceResolver,
        publisher: ComputeBundlePublisher,
        runner: SandboxRunner,
        *,
        limits: SandboxLimits | None = None,
    ) -> None:
        self.resolver = resolver
        self.publisher = publisher
        self.runner = runner
        self.limits = limits or SandboxLimits()

    def execute(self, context: ToolExecutionContext, arguments: dict[str, Any]) -> ToolResult:
        args = SandboxExecuteArgs.model_validate(arguments)
        state, request = self.resolver.analysis_request(context, args.analysis_id)
        resources: list[ResolvedResource] = []
        for item in args.inputs:
            if item.evidence_id:
                resources.append(
                    self.resolver.evidence(
                        context, args.analysis_id, item.evidence_id, item.name
                    )
                )
            else:
                assert item.dataset_ref
                resources.append(self.resolver.dataset(context, item.dataset_ref, item.name))
        evidence_ids = [item.evidence_id for item in resources if item.evidence_id]
        if not set(evidence_ids) <= set(request.input_evidence_ids):
            raise PermissionError("Sandbox inputs exceed the AnalysisRequest")
        by_id = {item.evidence_id: item for item in state.evidence_pool}
        evidence = [by_id[item] for item in evidence_ids]
        datasets = [item for item in resources if item.dataset_ref]
        code = args.code.encode("utf-8")
        with tempfile.TemporaryDirectory(prefix="deepresearch-sandbox-") as temporary:
            root = Path(temporary)
            inputs_dir = root / "inputs"
            outputs_dir = root / "outputs"
            inputs_dir.mkdir()
            outputs_dir.mkdir()
            os.chmod(outputs_dir, 0o777)
            input_manifest: list[dict[str, str]] = []
            for resource in resources:
                target = inputs_dir / resource.name
                target.write_bytes(resource.data)
                os.chmod(target, 0o444)
                manifest_entry = {
                    "name": resource.name,
                    "path": f"/inputs/{resource.name}",
                    "content_hash": resource.content_hash,
                }
                if resource.evidence_id:
                    manifest_entry["evidence_id"] = resource.evidence_id
                else:
                    assert resource.dataset_ref
                    manifest_entry["dataset_ref"] = resource.dataset_ref
                input_manifest.append(manifest_entry)
            manifest_file = inputs_dir / "_manifest.json"
            manifest_file.write_bytes(canonical_json({"inputs": input_manifest}))
            os.chmod(manifest_file, 0o444)
            code_path = root / "code.py"
            code_path.write_bytes(code)
            os.chmod(code_path, 0o444)
            tool_call_id = f"compute_{context.operation_key[:40]}"
            remaining_ms = int(
                (context.deadline - datetime.now(timezone.utc)).total_seconds() * 1000
            )
            if remaining_ms <= 0:
                raise TimeoutError("Sandbox deadline expired before execution")
            result = self.runner.run(
                SandboxSpec(
                    tool_call_id=tool_call_id,
                    code_path=code_path,
                    inputs_dir=inputs_dir,
                    outputs_dir=outputs_dir,
                    timeout_ms=min(
                        args.timeout_ms,
                        self.limits.max_timeout_ms,
                        remaining_ms,
                    ),
                    cancellation_token=context.cancellation_token,
                )
            )
            if result.exit_code != 0:
                error_type: ToolErrorType = (
                    "limit_exceeded" if result.limit_reason else "tool_error"
                )
                return ToolResult(
                    status="error",
                    summary="Sandbox execution failed",
                    data={
                        "exit_code": result.exit_code,
                        "stdout": result.stdout,
                        "stderr": result.stderr,
                        "truncated_output": result.truncated_output,
                        "limit_reason": result.limit_reason,
                    },
                    error=ToolError(
                        type=error_type,
                        code=result.limit_reason or "sandbox_exit_nonzero",
                        message="Sandbox process did not exit successfully.",
                    ),
                )
            output_payloads = _collect_outputs(outputs_dir, self.limits)
        bundle = self.publisher.publish(
            context,
            analysis_id=args.analysis_id,
            evidence=evidence,
            datasets=datasets,
            runtime=str(
                getattr(self.runner, "runtime_descriptor", "sandbox-runner")
            ),
            outputs=output_payloads,
            code=code,
            extra_manifest={
                "resource_usage": {
                    "runtime_ms": result.runtime_ms,
                    "peak_memory_bytes": result.peak_memory_bytes,
                }
            },
        )
        return ToolResult(
            status="success",
            summary=f"Sandbox produced {len(bundle.outputs)} output files",
            data={
                "exit_code": result.exit_code,
                "stdout": result.stdout,
                "stderr": result.stderr,
                "truncated_output": result.truncated_output,
                "lineage_manifest_path": bundle.manifest_path,
                "evidence_ids": evidence_ids,
                "files": [item.__dict__ for item in bundle.outputs],
                "resource_usage": {
                    "runtime_ms": result.runtime_ms,
                    "peak_memory_bytes": result.peak_memory_bytes,
                },
            },
            references=[
                ToolReference(
                    workspace_path=item.workspace_path,
                    content_hash=item.content_hash,
                    mime_type=item.media_type,
                    size=item.size,
                )
                for item in bundle.outputs
            ],
        )


class StructuredData(BaseModel):
    model_config = ConfigDict(extra="forbid")
    columns: list[str] = Field(min_length=1)
    rows: list[list[Any]]

    @model_validator(mode="after")
    def validate_shape(self) -> "StructuredData":
        if any(not column for column in self.columns) or len(self.columns) != len(
            set(self.columns)
        ):
            raise ValueError("columns must be non-empty and unique")
        if any(len(row) != len(self.columns) for row in self.rows):
            raise ValueError("each row must match the columns length")
        return self


class DataRef(BaseModel):
    model_config = ConfigDict(extra="forbid")
    workspace_path: str | None = None
    doc_id: str | None = None
    chunk_id: str | None = None

    @model_validator(mode="after")
    def validate_location(self) -> "DataRef":
        document = self.doc_id is not None or self.chunk_id is not None
        if (self.workspace_path is None) == (not document):
            raise ValueError("data_ref requires workspace_path xor doc_id+chunk_id")
        if (self.doc_id is None) != (self.chunk_id is None):
            raise ValueError("doc_id and chunk_id must be provided together")
        return self


class GenerateChartArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    chart_type: Literal["line", "bar", "pie", "scatter"]
    data: StructuredData | None = None
    data_ref: DataRef | None = None
    evidence_ids: list[str] = Field(min_length=1)
    x: str
    y: str | list[str]
    series: str | None = None
    title: str | None = None

    @model_validator(mode="after")
    def one_input(self) -> "GenerateChartArgs":
        if (self.data is None) == (self.data_ref is None):
            raise ValueError("chart requires data xor data_ref")
        return self


class GenerateChartAdapter:
    def __init__(
        self,
        resolver: ComputeResourceResolver,
        publisher: ComputeBundlePublisher,
        limits: Phase6Limits = DEFAULT_PHASE6_LIMITS,
    ) -> None:
        self.resolver = resolver
        self.publisher = publisher
        self.limits = limits

    def execute(self, context: ToolExecutionContext, arguments: dict[str, Any]) -> ToolResult:
        args = GenerateChartArgs.model_validate(arguments)
        analysis_id, evidence = self.resolver.validate_evidence_ids(
            context, args.evidence_ids
        )
        data = args.data or self.resolver.structured_data(
            context, args.data_ref, args.evidence_ids  # type: ignore[arg-type]
        )
        payload = _render_chart(args, data, self.limits)
        bundle = self.publisher.publish(
            context,
            analysis_id=analysis_id,
            evidence=evidence,
            datasets=[],
            runtime="matplotlib-agg-v1",
            outputs=[("chart.png", payload, "image/png")],
            extra_manifest={"chart_type": args.chart_type},
        )
        output = bundle.outputs[0]
        return ToolResult(
            status="success",
            summary=f"Rendered deterministic {args.chart_type} chart",
            data={
                "lineage_manifest_path": bundle.manifest_path,
                "evidence_ids": args.evidence_ids,
            },
            references=[
                ToolReference(
                    workspace_path=output.workspace_path,
                    content_hash=output.content_hash,
                    mime_type=output.media_type,
                    size=output.size,
                )
            ],
        )


class RenderTableArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    inline: StructuredData | None = None
    data_ref: DataRef | None = None
    evidence_ids: list[str] = Field(min_length=1)
    title: str | None = None
    format: Literal["markdown", "html", "csv"] = "markdown"
    max_rows: int = Field(
        default=DEFAULT_PHASE6_LIMITS.table_default_rows,
        gt=0,
        le=DEFAULT_PHASE6_LIMITS.table_max_rows,
    )

    @model_validator(mode="after")
    def one_input(self) -> "RenderTableArgs":
        if (self.inline is None) == (self.data_ref is None):
            raise ValueError("table requires inline xor data_ref")
        return self


class RenderTableAdapter:
    def __init__(
        self,
        resolver: ComputeResourceResolver,
        publisher: ComputeBundlePublisher,
        limits: Phase6Limits = DEFAULT_PHASE6_LIMITS,
    ) -> None:
        self.resolver = resolver
        self.publisher = publisher
        self.limits = limits

    def execute(self, context: ToolExecutionContext, arguments: dict[str, Any]) -> ToolResult:
        args = RenderTableArgs.model_validate(arguments)
        analysis_id, evidence = self.resolver.validate_evidence_ids(
            context, args.evidence_ids
        )
        data = args.inline or self.resolver.structured_data(
            context, args.data_ref, args.evidence_ids  # type: ignore[arg-type]
        )
        payload, rendered_rows, truncated = _render_table(args, data, self.limits)
        extension = {"markdown": "md", "html": "html", "csv": "csv"}[args.format]
        media_type = {
            "markdown": "text/markdown; charset=utf-8",
            "html": "text/html; charset=utf-8",
            "csv": "text/csv; charset=utf-8",
        }[args.format]
        bundle = self.publisher.publish(
            context,
            analysis_id=analysis_id,
            evidence=evidence,
            datasets=[],
            runtime="deterministic-table-v1",
            outputs=[(f"table.{extension}", payload, media_type)],
            extra_manifest={
                "source_row_count": len(data.rows),
                "rendered_row_count": rendered_rows,
                "truncated": truncated,
            },
        )
        output = bundle.outputs[0]
        return ToolResult(
            status="success",
            summary=f"Rendered {rendered_rows} of {len(data.rows)} rows",
            data={
                "source_row_count": len(data.rows),
                "rendered_row_count": rendered_rows,
                "truncated": truncated,
                "lineage_manifest_path": bundle.manifest_path,
                "evidence_ids": args.evidence_ids,
            },
            references=[
                ToolReference(
                    workspace_path=output.workspace_path,
                    content_hash=output.content_hash,
                    mime_type=output.media_type,
                    size=output.size,
                )
            ],
            truncated=truncated,
        )


def tool_definitions(
    *,
    resolver: ComputeResourceResolver,
    workspace: WorkspaceService,
    runner: SandboxRunner,
    limits: Phase6Limits = DEFAULT_PHASE6_LIMITS,
) -> list[ToolDefinition]:
    publisher = ComputeBundlePublisher(workspace)
    sandbox_limits = SandboxLimits.from_phase6(limits)
    return [
        ToolDefinition(
            name="sandbox_execute",
            args_schema=SandboxExecuteArgs,
            adapter=SandboxExecuteAdapter(
                resolver, publisher, runner, limits=sandbox_limits
            ),
            evidence_eligible=True,
            effect_target=lambda args: f"analysis:{args['analysis_id']}:sandbox",
            data_with_workspace_is_metadata=True,
        ),
        ToolDefinition(
            name="generate_chart",
            args_schema=GenerateChartArgs,
            adapter=GenerateChartAdapter(resolver, publisher, limits),
            evidence_eligible=True,
            effect_target=lambda args: sha256_hex(canonical_json(args)),
            data_with_workspace_is_metadata=True,
        ),
        ToolDefinition(
            name="render_table",
            args_schema=RenderTableArgs,
            adapter=RenderTableAdapter(resolver, publisher, limits),
            evidence_eligible=True,
            effect_target=lambda args: sha256_hex(canonical_json(args)),
            data_with_workspace_is_metadata=True,
        ),
    ]


def _safe_resource_name(name: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", name):
        raise ValueError("resource name must be one safe path segment")
    if name in {".", ".."}:
        raise ValueError("resource name is invalid")
    return name


def _safe_output_path(path: str) -> str:
    normalized = path.replace("\\", "/")
    candidate = Path(normalized)
    if candidate.is_absolute() or ".." in candidate.parts or not candidate.parts:
        raise ValueError("Sandbox output path is invalid")
    return "/".join(candidate.parts)


def _collect_outputs(
    output_dir: Path, limits: SandboxLimits
) -> list[tuple[str, bytes, str]]:
    collected: list[tuple[str, bytes, str]] = []
    total = 0
    root = output_dir.resolve()
    for path in sorted(output_dir.rglob("*"), key=lambda item: item.as_posix()):
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode):
            raise ValueError("Sandbox outputs cannot contain symlinks")
        if path.is_dir():
            continue
        if not stat.S_ISREG(info.st_mode):
            raise ValueError("Sandbox outputs must be regular files")
        resolved = path.resolve()
        if root not in resolved.parents:
            raise ValueError("Sandbox output escaped the output directory")
        if info.st_size > limits.single_output_bytes:
            raise ValueError("Sandbox output exceeds the single-file limit")
        total += info.st_size
        if total > limits.total_output_bytes:
            raise ValueError("Sandbox outputs exceed the total output limit")
        relative = _safe_output_path(path.relative_to(output_dir).as_posix())
        media_type = mimetypes.guess_type(relative)[0] or "application/octet-stream"
        collected.append((relative, path.read_bytes(), media_type))
    return collected

def _parse_structured(payload: bytes, name: str) -> StructuredData:
    suffix = name.lower().rsplit(".", 1)[-1] if "." in name else ""
    if suffix == "csv":
        rows = list(csv.reader(io.StringIO(payload.decode("utf-8-sig"))))
        if not rows:
            raise ValueError("structured data is empty")
        return StructuredData(columns=rows[0], rows=rows[1:])
    if suffix in {"parquet", "pq", "xlsx", "xls"}:
        import pandas as pd

        buffer = io.BytesIO(payload)
        frame = pd.read_parquet(buffer) if suffix in {"parquet", "pq"} else pd.read_excel(buffer)
        return StructuredData(
            columns=[str(item) for item in frame.columns],
            rows=[[None if value != value else value for value in row] for row in frame.values.tolist()],
        )
    parsed = json.loads(payload.decode("utf-8"))
    if isinstance(parsed, dict) and "columns" in parsed and "rows" in parsed:
        return StructuredData.model_validate(parsed)
    if isinstance(parsed, list) and (not parsed or isinstance(parsed[0], dict)):
        columns = list(parsed[0]) if parsed else ["value"]
        return StructuredData(
            columns=columns,
            rows=[[item.get(column) for column in columns] for item in parsed],
        )
    raise ValueError("data_ref is not supported structured data")


def _render_chart(
    args: GenerateChartArgs,
    data: StructuredData,
    limits: Phase6Limits = DEFAULT_PHASE6_LIMITS,
) -> bytes:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    columns = {name: index for index, name in enumerate(data.columns)}
    y_columns = [args.y] if isinstance(args.y, str) else args.y
    required = {args.x, *y_columns, *([args.series] if args.series else [])}
    missing = required - set(columns)
    if missing:
        raise ValueError(f"unknown chart columns: {sorted(missing)}")
    if args.chart_type in {"pie", "scatter"} and len(y_columns) != 1:
        raise ValueError(f"{args.chart_type} requires one y column")
    point_count = len(data.rows) * len(y_columns)
    if point_count > limits.chart_max_points:
        raise ValueError(
            f"chart exceeds the {limits.chart_max_points:,} point hard limit"
        )
    categories = {str(row[columns[args.x]]) for row in data.rows}
    if len(categories) > limits.chart_max_categories:
        raise ValueError(
            f"chart exceeds the {limits.chart_max_categories} category hard limit"
        )
    series_values: list[str | None] = []
    if args.series:
        series_values.extend(
            sorted({str(row[columns[args.series]]) for row in data.rows})
        )
    else:
        series_values.append(None)
    if len(series_values) > limits.chart_max_series:
        raise ValueError(
            f"chart exceeds the {limits.chart_max_series} series hard limit"
        )

    figure, axis = plt.subplots(figsize=(8, 4.5), dpi=100)
    if args.chart_type == "pie":
        values = [_number(row[columns[y_columns[0]]]) for row in data.rows]
        axis.pie(values, labels=[str(row[columns[args.x]]) for row in data.rows])
    else:
        for series_value in series_values:
            rows = (
                [row for row in data.rows if str(row[columns[args.series]]) == series_value]
                if args.series
                else data.rows
            )
            x_values = [row[columns[args.x]] for row in rows]
            for y_name in y_columns:
                values = [_number(row[columns[y_name]]) for row in rows]
                label = "/".join(
                    item for item in (str(series_value) if series_value else "", y_name) if item
                )
                if args.chart_type == "line":
                    axis.plot(x_values, values, marker="o", label=label)
                elif args.chart_type == "bar":
                    axis.bar([str(item) for item in x_values], values, label=label)
                else:
                    axis.scatter(x_values, values, label=label)
        if len(series_values) * len(y_columns) > 1:
            axis.legend()
        axis.set_xlabel(args.x)
        axis.set_ylabel(", ".join(y_columns))
    if args.title:
        axis.set_title(args.title)
    figure.tight_layout()
    buffer = io.BytesIO()
    figure.savefig(
        buffer,
        format="png",
        dpi=100,
        metadata={"Software": "DeepResearch deterministic chart v1"},
    )
    plt.close(figure)
    return buffer.getvalue()


def _number(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("chart y values must be numeric")
    return float(value)


def _render_table(
    args: RenderTableArgs,
    data: StructuredData,
    limits: Phase6Limits = DEFAULT_PHASE6_LIMITS,
) -> tuple[bytes, int, bool]:
    truncated_columns = [
        _truncate_utf8(item, limits.table_cell_max_bytes)
        for item in data.columns[: limits.table_max_columns]
    ]
    columns = [item[0] for item in truncated_columns]
    truncated = (
        len(data.columns) > limits.table_max_columns
        or len(data.rows) > args.max_rows
        or any(item[1] for item in truncated_columns)
    )
    rows = [
        [
            _truncate_utf8(
                "" if value is None else str(value), limits.table_cell_max_bytes
            )[0]
            for value in row[: limits.table_max_columns]
        ]
        for row in data.rows[: args.max_rows]
    ]
    truncated = truncated or any(
        _truncate_utf8(
            "" if value is None else str(value), limits.table_cell_max_bytes
        )[1]
        for row in data.rows[: args.max_rows]
        for value in row[: limits.table_max_columns]
    )

    def serialize(selected: list[list[str]], marker: bool) -> bytes:
        if args.format == "csv":
            stream = io.StringIO(newline="")
            writer = csv.writer(stream, lineterminator="\n")
            writer.writerow(columns)
            writer.writerows(selected)
            if marker:
                writer.writerow(["[TRUNCATED]", *([""] * max(0, len(columns) - 1))])
            return stream.getvalue().encode("utf-8")
        if args.format == "html":
            title = f"<caption>{html.escape(args.title)}</caption>" if args.title else ""
            head = "".join(f"<th>{html.escape(item)}</th>" for item in columns)
            body = "".join(
                "<tr>" + "".join(f"<td>{html.escape(value)}</td>" for value in row) + "</tr>"
                for row in selected
            )
            marker_row = (
                f'<tr><td colspan="{len(columns)}">[TRUNCATED]</td></tr>' if marker else ""
            )
            return (
                f"<table>{title}<thead><tr>{head}</tr></thead>"
                f"<tbody>{body}{marker_row}</tbody></table>"
            ).encode("utf-8")
        escaped_columns = [_escape_markdown(item) for item in columns]
        lines = []
        if args.title:
            lines.extend([f"### {_escape_markdown(args.title)}", ""])
        lines.append("| " + " | ".join(escaped_columns) + " |")
        lines.append("| " + " | ".join(["---"] * len(columns)) + " |")
        lines.extend(
            "| " + " | ".join(_escape_markdown(value) for value in row) + " |"
            for row in selected
        )
        if marker:
            lines.append("| " + " | ".join(["**[TRUNCATED]**", *([""] * (len(columns) - 1))]) + " |")
        return ("\n".join(lines) + "\n").encode("utf-8")

    while True:
        payload = serialize(rows, truncated)
        if len(payload) <= limits.table_serialized_max_bytes:
            return payload, len(rows), truncated
        if not rows:
            raise ValueError("table headers exceed the serialized size limit")
        rows.pop()
        truncated = True


def _truncate_utf8(value: str, limit: int) -> tuple[str, bool]:
    encoded = value.encode("utf-8")
    if len(encoded) <= limit:
        return value, False
    marker = "…[truncated]"
    budget = limit - len(marker.encode("utf-8"))
    return encoded[:budget].decode("utf-8", errors="ignore") + marker, True


def _escape_markdown(value: str) -> str:
    return value.replace("\\", "\\\\").replace("|", "\\|").replace("\n", "<br>")
