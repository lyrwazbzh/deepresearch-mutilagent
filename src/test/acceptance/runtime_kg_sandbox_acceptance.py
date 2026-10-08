from __future__ import annotations

import hashlib
import json
import os
import signal
import tempfile
import time
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import func, select

from src.agent.tools.compute import (
    ComputeBundlePublisher,
    DockerSandboxRunner,
    SandboxSpec,
)
from src.agent.tools.documents import ElasticsearchDocumentIndex, IngestionService
from src.agent.config import Settings
from src.agent.container import build_container
from src.agent.schema.runtime import StatePatch
from src.agent.schema.state import Evidence
from src.agent.tools.knowledge_graph import KnowledgeGraphQuery, Neo4jKnowledgeGraphStore
from src.agent.backends.persistence.models import (
    NodeEffectRow,
    NodeRunRow,
    OutboxMessageRow,
    ResearchCheckpointRow,
    ResearchTaskRow,
    TaskKnowledgeGraphRow,
    WorkspaceFileRow,
    utcnow,
)
from src.agent.runtime.tool_runtime import ToolExecutionContext
from src.agent.errors import LeaseLostError
from src.agent.task_service import CreateTaskOptions, CreateTaskRequest, new_id
from src.agent.workers.celery_app import celery_app
from src.agent.workers.recovery import RuntimeRecoveryScanner
from src.agent.backends.workspace import MinioObjectStore


def emit(section: str, payload: dict[str, Any]) -> None:
    print(json.dumps({"section": section, **payload}, default=str, sort_keys=True), flush=True)


def find_dispatcher_pid() -> int:
    matches: list[int] = []
    for proc in Path("/proc").iterdir():
        if not proc.name.isdigit():
            continue
        try:
            command = (proc / "cmdline").read_bytes().split(b"\0")
        except (FileNotFoundError, PermissionError, ProcessLookupError):
            continue
        args = [item.decode(errors="replace") for item in command if item]
        if len(args) >= 3 and args[-2:] == ["-m", "src.agent.workers.dispatcher_main"]:
            matches.append(int(proc.name))
    if len(matches) != 1:
        raise RuntimeError(f"Expected one dispatcher process, found {matches}")
    return matches[0]


def wait_for(predicate: Any, *, timeout: float, label: str) -> Any:
    deadline = time.monotonic() + timeout
    last: Any = None
    while time.monotonic() < deadline:
        last = predicate()
        if last:
            return last
        time.sleep(0.25)
    raise TimeoutError(f"Timed out waiting for {label}; last={last!r}")


class NeverCancelled:
    def is_cancelled(self) -> bool:
        return False


class DeterministicAcceptanceVectors:
    """Keeps the storage acceptance independent of an external embedding account."""

    def __init__(self, dimensions: int) -> None:
        self.dimensions = dimensions

    def embed(self, _tenant_id: str, _binding: Any, texts: list[str]) -> list[list[float]]:
        vectors: list[list[float]] = []
        for text in texts:
            digest = hashlib.sha256(text.encode()).digest()
            vectors.append(
                [((digest[index % len(digest)] / 255.0) * 2.0) - 1.0 for index in range(self.dimensions)]
            )
        return vectors

    def rerank(
        self,
        _tenant_id: str,
        _binding: Any,
        _query: str,
        candidates: list[str],
    ) -> list[float]:
        return [1.0 / (index + 1) for index, _candidate in enumerate(candidates)]


def create_task(container: Any, label: str, query: str) -> str:
    settings = container.settings
    created = container.task_service.create(
        settings.default_tenant_id,
        settings.default_user_id,
        f"phase9-real-{label}-{time.time_ns()}",
        CreateTaskRequest(
            query=query,
            options=CreateTaskOptions(max_depth=1, max_revision_rounds=1),
        ),
    )
    return created.projection.task_id


def add_running_node(
    container: Any,
    task_id: str,
    *,
    node_name: str,
    node_key: str,
) -> tuple[str, str]:
    with container.session_factory() as session, session.begin():
        checkpoint = session.scalar(
            select(ResearchCheckpointRow)
            .where(ResearchCheckpointRow.task_id == task_id)
            .order_by(ResearchCheckpointRow.checkpoint_seq.desc())
            .limit(1)
        )
        if checkpoint is None:
            raise RuntimeError("Task has no checkpoint")
        node_run_id = new_id("node")
        session.add(
            NodeRunRow(
                node_run_id=node_run_id,
                tenant_id=container.settings.default_tenant_id,
                task_id=task_id,
                node_key=node_key,
                node_name=node_name,
                input_checkpoint_id=checkpoint.checkpoint_id,
                status="running",
                attempt_count=1,
            )
        )
        return node_run_id, checkpoint.checkpoint_id


def pause_resume_cancel(container: Any) -> dict[str, Any]:
    tenant = container.settings.default_tenant_id
    user = container.settings.default_user_id
    task_id = create_task(container, "controls", "Phase 9 runtime control acceptance")
    _, first_lease = container.worker.claim(task_id)
    with container.session_factory() as session:
        before = session.get(ResearchTaskRow, task_id).leased_until
    time.sleep(0.05)
    heartbeat = container.worker.refresh_lease(task_id, first_lease)
    with container.session_factory() as session:
        after = session.get(ResearchTaskRow, task_id).leased_until
    paused_requested = container.task_service.command(
        tenant, user, task_id, "pause", f"pause-{time.time_ns()}"
    )
    container.worker._finish(task_id, first_lease, "paused", None)
    resumed = container.task_service.command(
        tenant, user, task_id, "resume", f"resume-{time.time_ns()}"
    )
    _, second_lease = container.worker.claim(task_id)
    cancelling = container.task_service.command(
        tenant, user, task_id, "cancel", f"cancel-{time.time_ns()}"
    )
    container.worker._finish(task_id, second_lease, "cancelled", None)
    final = container.task_service.get(tenant, user, task_id)
    return {
        "task_id": task_id,
        "heartbeat_refreshed": heartbeat and after is not None and before is not None and after > before,
        "pause_requested": paused_requested.task_status.value,
        "resume_requested": resumed.task_status.value,
        "cancel_requested": cancelling.task_status.value,
        "final_status": final.task_status.value,
        "new_lease_after_resume": first_lease != second_lease,
    }


def recovery_and_duplicate_delivery(container: Any) -> dict[str, Any]:
    task_id = create_task(
        container,
        "recovery",
        "Continue from the durable route checkpoint and answer in one sentence.",
    )
    claimed, old_lease = container.worker.claim(task_id)
    frozen_before = dict(claimed.model_binding_snapshot)
    route_run, _ = add_running_node(
        container, task_id, node_name="route", node_key="route"
    )
    checkpoint = container.state_manager.commit_node(
        route_run,
        StatePatch(values={"route": {"mode": "direct", "target_agent": None}}),
        old_lease,
        output={"route": {"mode": "direct", "target_agent": None}},
    )

    # Real duplicate Celery delivery while the activation is already owned. Both
    # deliveries must become domain no-ops rather than gaining a second lease.
    celery_app.send_task("research.run", args=[task_id], queue="research")
    celery_app.send_task("research.run", args=[task_id], queue="research")
    time.sleep(1.5)
    with container.session_factory() as session:
        still_owned = session.get(ResearchTaskRow, task_id)
        duplicate_arbitrated = (
            still_owned is not None
            and still_owned.status == "running"
            and still_owned.lease_token == old_lease
        )

    now = utcnow()
    with container.session_factory() as session, session.begin():
        task = session.get(ResearchTaskRow, task_id)
        if task is None:
            raise RuntimeError("Recovery task disappeared")
        task.leased_until = now - timedelta(seconds=1)
    repaired = RuntimeRecoveryScanner(container.session_factory).scan_once(now=now)
    with container.session_factory() as session:
        pending = session.get(ResearchTaskRow, task_id)
        recovery_outbox = session.scalar(
            select(OutboxMessageRow).where(
                OutboxMessageRow.aggregate_id == task_id,
                OutboxMessageRow.kind == "research.schedule",
                OutboxMessageRow.idempotency_key.like("recovery:%"),
            )
        )
    if pending is None or pending.status != "pending" or recovery_outbox is None:
        raise RuntimeError("Expired activation did not converge to pending + recovery Outbox")

    return {
        "task_id": task_id,
        "old_lease": old_lease,
        "route_run": route_run,
        "route_checkpoint": checkpoint.checkpoint_id,
        "frozen_before": frozen_before,
        "duplicate_arbitrated": duplicate_arbitrated,
        "repaired": repaired,
        "recovery_outbox_id": recovery_outbox.message_id,
    }


def finish_recovery(container: Any, setup: dict[str, Any]) -> dict[str, Any]:
    task_id = setup["task_id"]

    def terminal() -> bool:
        with container.session_factory() as session:
            row = session.get(ResearchTaskRow, task_id)
            return bool(row and row.status in {"completed", "failed"})

    wait_for(terminal, timeout=60, label="recovered task terminal state")
    fenced = False
    try:
        container.state_manager.commit_node(
            setup["route_run"], StatePatch(), setup["old_lease"]
        )
    except LeaseLostError:
        fenced = True

    with container.session_factory() as session:
        task = session.get(ResearchTaskRow, task_id)
        nodes = session.scalars(
            select(NodeRunRow)
            .where(NodeRunRow.task_id == task_id)
            .order_by(NodeRunRow.created_at)
        ).all()
        effects = session.scalars(
            select(NodeEffectRow).where(NodeEffectRow.task_id == task_id)
        ).all()
        schedules = session.scalars(
            select(OutboxMessageRow).where(
                OutboxMessageRow.aggregate_id == task_id,
                OutboxMessageRow.kind == "research.schedule",
            )
        ).all()
        assert task is not None
        return {
            "task_id": task_id,
            "status": task.status,
            "old_worker_fenced": fenced,
            "binding_unchanged": task.model_binding_snapshot == setup["frozen_before"],
            "node_runs": [
                {"name": row.node_name, "status": row.status, "attempts": row.attempt_count}
                for row in nodes
            ],
            "effect_count": len(effects),
            "operation_keys_unique": len({row.operation_key for row in effects}) == len(effects),
            "schedule_outboxes": len(schedules),
            "all_schedules_dispatched": all(row.dispatched_at is not None for row in schedules),
            "latest_checkpoint": session.scalar(
                select(func.max(ResearchCheckpointRow.checkpoint_seq)).where(
                    ResearchCheckpointRow.task_id == task_id
                )
            ),
            "budget": task.task_budget,
        }


def document_flow(container: Any) -> dict[str, Any]:
    tenant = container.settings.default_tenant_id
    data = (
        b"Phase Nine acceptance document. Acme reported revenue of USD 10 million "
        b"for fiscal year 2025. This sentence supports retrieval verification."
    )
    doc_id = container.document_service.create(
        tenant_id=tenant,
        name=f"phase9-real-{time.time_ns()}.txt",
        media_type="text/plain",
        data=data,
        tags=["phase9-real"],
    )
    with container.session_factory() as session:
        embedding = container.model_resolver.resolve_snapshot(
            session, tenant, ("embedding",)
        ).get("embedding")
    generation = IngestionService(
        container.session_factory,
        container.document_service,
        DeterministicAcceptanceVectors(container.settings.openai_embedding_dimensions),
        embedding,
    ).run(tenant, doc_id)
    generations = container.document_service.generations(tenant)
    hits = container.document_service.index.bm25(
        "Acme revenue 2025",
        tenant_id=tenant,
        generations=generations,
        doc_ids={doc_id},
        tags=None,
        limit=5,
    )
    document, original = container.document_service.read_original(tenant, doc_id)
    return {
        "doc_id": doc_id,
        "generation": generation,
        "document_status": document.status,
        "minio_backend": type(container.document_service.object_store).__name__,
        "elasticsearch_backend": type(container.document_service.index).__name__,
        "embedding_path": "deterministic_acceptance_vector",
        "original_roundtrip": original == data,
        "search_hits": len(hits),
        "hit_doc_ids": sorted({item.doc_id for item, _ in hits}),
    }


def merge_evidence_for_kg(container: Any) -> tuple[str, Evidence]:
    task_id = create_task(
        container,
        "kg",
        "What revenue did Acme report for fiscal year 2025?",
    )
    _, lease = container.worker.claim(task_id)
    node_run, _ = add_running_node(
        container,
        task_id,
        node_name="merge_evidence",
        node_key="merge_evidence:phase9-real",
    )
    payload = {
        "company": "Acme",
        "metric": "revenue",
        "value": "USD 10 million",
        "fiscal_year": 2025,
    }
    evidence = Evidence(
        evidence_id=f"ev_phase9_{hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()[:20]}",
        hypothesis_id="hyp_phase9_real",
        summary="Acme reported revenue of USD 10 million for fiscal year 2025.",
        excerpt="Acme revenue: USD 10 million; fiscal year: 2025.",
        source_type="sql",
        stance="support",
        relevance=1.0,
        data=payload,
        content_hash=hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest(),
    )
    container.state_manager.commit_node(
        node_run,
        StatePatch(list_appends={"evidence_pool": [evidence.model_dump(mode="json")]}),
        lease,
        output={"merged_evidence_ids": [evidence.evidence_id]},
    )
    container.worker._finish(task_id, lease, "completed", None)
    with container.session_factory() as session:
        task = session.get(ResearchTaskRow, task_id)
        metadata = session.get(
            TaskKnowledgeGraphRow,
            (container.settings.default_tenant_id, task_id),
        )
        outbox = session.scalar(
            select(OutboxMessageRow).where(
                OutboxMessageRow.aggregate_id == task_id,
                OutboxMessageRow.kind == "knowledge_graph.build",
            )
        )
        if task is None or metadata is None or outbox is None:
            raise RuntimeError("MergeEvidence did not atomically create KG lifecycle + Outbox")
        if len(task.latest_state.get("evidence_pool") or []) != 1:
            raise RuntimeError("MergeEvidence did not commit formal Evidence")
    return task_id, evidence


def wait_for_kg(container: Any, task_id: str, evidence_id: str) -> dict[str, Any]:
    tenant = container.settings.default_tenant_id

    def ready() -> bool:
        with container.session_factory() as session:
            metadata = session.get(TaskKnowledgeGraphRow, (tenant, task_id))
            return bool(
                metadata
                and metadata.status in {"ready", "partial"}
                and metadata.processed_evidence_count >= 1
            )

    wait_for(ready, timeout=60, label="Task KG ready")
    before_count: int
    before_budget: dict[str, Any]
    with container.session_factory() as session:
        task = session.get(ResearchTaskRow, task_id)
        assert task is not None
        before_count = len(task.latest_state.get("evidence_pool") or [])
        before_budget = dict(task.task_budget)
    query = container.knowledge_graph_service.query(
        tenant,
        container.settings.default_user_id,
        task_id,
        KnowledgeGraphQuery(op="search", query="revenue", limit=20),
    )
    with container.session_factory() as session:
        task = session.get(ResearchTaskRow, task_id)
        metadata = session.get(TaskKnowledgeGraphRow, (tenant, task_id))
        build_nodes = session.scalars(
            select(NodeRunRow).where(
                NodeRunRow.task_id == task_id,
                NodeRunRow.node_name == "knowledge_graph_build",
            )
        ).all()
        effects = session.scalars(
            select(NodeEffectRow).where(NodeEffectRow.task_id == task_id)
        ).all()
        assert task is not None and metadata is not None
        evidence_unchanged = len(task.latest_state.get("evidence_pool") or []) == before_count
        provenance = sorted(
            {
                item.evidence_id
                for claim in query.claims
                for item in claim.provenance
            }
        )
        return {
            "task_id": task_id,
            "status": metadata.status,
            "processed_evidence_count": metadata.processed_evidence_count,
            "query_claims": len(query.claims),
            "query_entities": len(query.entities),
            "query_evidence_ids": query.evidence_ids,
            "claim_provenance": provenance,
            "expected_evidence_id": evidence_id,
            "evidence_pool_unchanged": evidence_unchanged,
            "query_hash": metadata.query_hash,
            "plan_hash": metadata.plan_hash,
            "build_node_ids": [row.node_run_id for row in build_nodes],
            "build_attempts": [row.attempt_count for row in build_nodes],
            "effect_count": len(effects),
            "budget_before_rebuild": before_budget,
        }


def schedule_kg_rebuild(container: Any, task_id: str) -> None:
    tenant = container.settings.default_tenant_id
    container.knowledge_graph_service.store.clear(tenant, task_id)
    with container.session_factory() as session, session.begin():
        metadata = session.get(TaskKnowledgeGraphRow, (tenant, task_id))
        if metadata is None:
            raise RuntimeError("KG metadata disappeared")
        metadata.status = "building"
        metadata.processed_evidence_count = 0
        metadata.updated_at = utcnow()
        session.add(
            OutboxMessageRow(
                message_id=new_id("outbox"),
                tenant_id=tenant,
                kind="knowledge_graph.build",
                aggregate_id=task_id,
                payload_json={"task_id": task_id},
                idempotency_key=f"kg:real-rebuild:{task_id}:{time.time_ns()}",
                available_at=utcnow(),
            )
        )


def verify_kg_rebuild(container: Any, task_id: str, before: dict[str, Any]) -> dict[str, Any]:
    tenant = container.settings.default_tenant_id

    def ready() -> bool:
        with container.session_factory() as session:
            metadata = session.get(TaskKnowledgeGraphRow, (tenant, task_id))
            return bool(
                metadata
                and metadata.status in {"ready", "partial"}
                and metadata.processed_evidence_count >= 1
            )

    wait_for(ready, timeout=60, label="rebuilt Task KG ready")
    query = container.knowledge_graph_service.query(
        tenant,
        container.settings.default_user_id,
        task_id,
        KnowledgeGraphQuery(op="search", query="revenue", limit=20),
    )
    with container.session_factory() as session:
        task = session.get(ResearchTaskRow, task_id)
        nodes = session.scalars(
            select(NodeRunRow).where(
                NodeRunRow.task_id == task_id,
                NodeRunRow.node_name == "knowledge_graph_build",
            )
        ).all()
        effects = session.scalars(
            select(NodeEffectRow).where(NodeEffectRow.task_id == task_id)
        ).all()
        assert task is not None
        return {
            "task_id": task_id,
            "query_claims": len(query.claims),
            "query_evidence_ids": query.evidence_ids,
            "build_node_ids": [row.node_run_id for row in nodes],
            "build_attempts": [row.attempt_count for row in nodes],
            "effect_count_before": before["effect_count"],
            "effect_count_after": len(effects),
            "budget_unchanged": task.task_budget == before["budget_before_rebuild"],
            "evidence_pool_count": len(task.latest_state.get("evidence_pool") or []),
        }


def sandbox_flow(container: Any, task_id: str) -> dict[str, Any]:
    runner = DockerSandboxRunner(
        image=container.settings.sandbox_image,
        runtime=container.settings.sandbox_runtime,
    )
    code = (
        "import os\n"
        "from pathlib import Path\n"
        "for key in ('DATABASE_URL','REDIS_URL','CELERY_BROKER_URL','MINIO_SECRET_KEY','OPENAI_API_KEY'):\n"
        "    assert key not in os.environ, key\n"
        "try:\n"
        "    Path('/work/forbidden.txt').write_text('no')\n"
        "    raise AssertionError('readonly rootfs was writable')\n"
        "except OSError:\n"
        "    pass\n"
        "Path('/outputs/result.txt').write_text('sandbox-runsc-ok', encoding='utf-8')\n"
        "print('sandbox-finished')\n"
    )
    with tempfile.TemporaryDirectory(prefix="phase9-sandbox-") as root:
        root_path = Path(root)
        inputs = root_path / "inputs"
        outputs = root_path / "outputs"
        inputs.mkdir()
        outputs.mkdir()
        (inputs / "_manifest.json").write_text("{}", encoding="utf-8")
        code_path = root_path / "code.py"
        code_path.write_text(code, encoding="utf-8")
        result = runner.run(
            SandboxSpec(
                tool_call_id=f"phase9-real-{time.time_ns()}",
                code_path=code_path,
                inputs_dir=inputs,
                outputs_dir=outputs,
                timeout_ms=60_000,
                cancellation_token=NeverCancelled(),
            )
        )
        output = (outputs / "result.txt").read_bytes()

    operation_key = hashlib.sha256(f"phase9-sandbox:{task_id}".encode()).hexdigest()
    context = ToolExecutionContext(
        tenant_id=container.settings.default_tenant_id,
        user_id=container.settings.default_user_id,
        task_id=task_id,
        thread_id="phase9-real",
        node_run_id="phase9-real-sandbox",
        cancellation_token=NeverCancelled(),
        deadline=utcnow() + timedelta(minutes=2),
        inline_budget=32_000,
        operation_key=operation_key,
    )
    publisher = ComputeBundlePublisher(container.workspace_service)
    first = publisher.publish(
        context,
        analysis_id="analysis_phase9_real",
        evidence=[],
        datasets=[],
        runtime=runner.runtime_descriptor,
        outputs=[("result.txt", output, "text/plain")],
        code=code.encode(),
    )
    replay = publisher.publish(
        context,
        analysis_id="analysis_phase9_real",
        evidence=[],
        datasets=[],
        runtime=runner.runtime_descriptor,
        outputs=[("result.txt", output, "text/plain")],
        code=code.encode(),
    )
    with container.session_factory() as session:
        workspace_rows = session.scalar(
            select(func.count())
            .select_from(WorkspaceFileRow)
            .where(WorkspaceFileRow.task_id == task_id)
        )
    stored = container.workspace_service.read(
        container.settings.default_tenant_id,
        task_id,
        first.outputs[0].workspace_path,
    )
    return {
        "exit_code": result.exit_code,
        "stdout": result.stdout.strip(),
        "runtime_descriptor": runner.runtime_descriptor,
        "workspace_backend": type(container.workspace_service.object_store).__name__,
        "workspace_output_roundtrip": stored == output,
        "workspace_rows": workspace_rows,
        "operation_replay_same_manifest": first.manifest_path == replay.manifest_path,
    }


def main() -> None:
    settings = Settings.from_env()
    if settings.model_mode != "configured":
        raise RuntimeError("Real acceptance requires the configured provider path")
    real_settings = replace(
        settings,
        minio_endpoint=settings.minio_endpoint or "localhost:9000",
        elasticsearch_url=settings.elasticsearch_url or "http://localhost:9200",
    )
    container = build_container(real_settings)
    if not isinstance(container.workspace_service.object_store, MinioObjectStore):
        raise RuntimeError("Workspace is not using MinIO")
    if not isinstance(container.document_service.object_store, MinioObjectStore):
        raise RuntimeError("Documents are not using MinIO")
    if not isinstance(container.document_service.index, ElasticsearchDocumentIndex):
        raise RuntimeError("Documents are not using Elasticsearch")
    if not isinstance(container.knowledge_graph_service.store, Neo4jKnowledgeGraphStore):
        raise RuntimeError("Task KG is not using Neo4j")

    dispatcher_pid = find_dispatcher_pid()
    os.kill(dispatcher_pid, signal.SIGSTOP)
    dispatcher_resumed = False
    try:
        emit("runtime_controls", pause_resume_cancel(container))
        recovery = recovery_and_duplicate_delivery(container)
        emit(
            "recovery_setup",
            {key: value for key, value in recovery.items() if key != "frozen_before"},
        )
        emit("document", document_flow(container))
        kg_task_id, evidence = merge_evidence_for_kg(container)
        emit(
            "merge_evidence",
            {"task_id": kg_task_id, "evidence_id": evidence.evidence_id},
        )
        emit("sandbox", sandbox_flow(container, kg_task_id))
        os.kill(dispatcher_pid, signal.SIGCONT)
        dispatcher_resumed = True

        emit("runtime_recovery", finish_recovery(container, recovery))
        first_kg = wait_for_kg(container, kg_task_id, evidence.evidence_id)
        emit("kg_build_query", first_kg)
        schedule_kg_rebuild(container, kg_task_id)
        emit("kg_rebuild", verify_kg_rebuild(container, kg_task_id, first_kg))
    finally:
        if not dispatcher_resumed:
            os.kill(dispatcher_pid, signal.SIGCONT)


if __name__ == "__main__":
    main()
