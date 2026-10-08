"""Prepare one real Task's read models for Phase 11 browser acceptance.

This is test setup, not a production state transition. It deliberately reuses the
frozen domain DTOs plus the configured PostgreSQL, MinIO and Neo4j stores so the
browser still exercises only public Phase 10 HTTP resources.
"""

from __future__ import annotations

import argparse
import hashlib
import json

from sqlalchemy import delete, select

from src.agent.container import build_container
from src.agent.schema.report import (
    Citation,
    ClaimSegment,
    ImageBlock,
    ParagraphBlock,
    ReportAst,
    Section,
    TextSegment,
)
from src.agent.schema.state import Evidence, ReportArtifact, ResearchState, RouteDecision
from src.agent.tools.knowledge_graph import (
    GraphClaim,
    GraphEntity,
    GraphProvenance,
    _question_hashes,
)
from src.agent.backends.persistence.models import (
    ConversationMessageRow,
    PendingWriteRow,
    ResearchCheckpointRow,
    ResearchTaskRow,
    TaskKnowledgeGraphRow,
    ThreadRow,
    WorkspaceFileRow,
    utcnow,
)
from src.agent.task_service import new_id
from src.agent.backends.persistence.repositories import CheckpointRepository


def digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--expected-user-id", required=True)
    parser.add_argument("--pending-task-id")
    args = parser.parse_args()

    container = build_container()
    with container.session_factory() as session:
        task = session.get(ResearchTaskRow, args.task_id)
        if task is None or task.user_id != args.expected_user_id:
            raise SystemExit("Fixture target is missing or owned by another user.")
        tenant_id = task.tenant_id
        thread_id = task.thread_id
        state = ResearchState.model_validate(task.latest_state)

    chart_operation = digest(f"phase11:{args.task_id}:chart".encode())
    report_operation = digest(f"phase11:{args.task_id}:report".encode())
    # A previous interrupted acceptance run may leave a ready metadata row after
    # its test object was cleaned up. These keys belong exclusively to this fixture.
    with container.session_factory() as session, session.begin():
        session.execute(
            delete(WorkspaceFileRow).where(
                WorkspaceFileRow.tenant_id == tenant_id,
                WorkspaceFileRow.task_id == args.task_id,
                WorkspaceFileRow.operation_key.in_([chart_operation, report_operation]),
            )
        )

    chart = """<svg xmlns="http://www.w3.org/2000/svg" width="900" height="420" viewBox="0 0 900 420">
<rect width="900" height="420" fill="#f7f7f0"/><text x="55" y="60" font-family="sans-serif" font-size="24" fill="#153e2d">Evidence balance</text>
<rect x="100" y="125" width="570" height="64" rx="8" fill="#2e7754"/><text x="115" y="165" font-family="sans-serif" font-size="20" fill="white">Supported · 78%</text>
<rect x="100" y="240" width="255" height="64" rx="8" fill="#d19b49"/><text x="115" y="280" font-family="sans-serif" font-size="20" fill="#173225">Context · 35%</text>
<text x="100" y="355" font-family="sans-serif" font-size="15" fill="#607168">Phase 11 browser acceptance fixture — deterministic test artifact</text></svg>""".encode()
    chart_file = container.workspace_service.publish_bytes(
        tenant_id=tenant_id,
        task_id=args.task_id,
        path="visualizations/evidence-balance.svg",
        data=chart,
        mime_type="image/svg+xml",
        operation_key=chart_operation,
    )

    retrieved_at = utcnow()
    evidence = [
        Evidence(
            evidence_id="evidence_phase11_web",
            hypothesis_id="hyp_phase11",
            summary="The public design contract requires server-authoritative resource projections.",
            excerpt="The browser refreshes Task, Evidence, Graph, Workspace and Report resources after SSE notifications.",
            source_type="web",
            stance="support",
            relevance=0.98,
            data={"title": "Phase 11 research source"},
            source_uri="https://example.com/research-source",
            retrieved_at=retrieved_at,
            content_hash=digest(b"phase11-web-evidence"),
        ),
        Evidence(
            evidence_id="evidence_phase11_inline",
            hypothesis_id="hyp_phase11",
            summary="The observed acceptance matrix covers the complete structured renderer.",
            excerpt="Paragraph, list, table, image, claim and citation paths are present.",
            source_type="sql",
            stance="context",
            relevance=0.91,
            data={"renderer_blocks": 4, "citation_resolution": "evidence_id"},
            content_hash=digest(b"phase11-inline-evidence"),
        ),
        Evidence(
            evidence_id="evidence_phase11_workspace",
            hypothesis_id="hyp_phase11",
            summary="A generated visualization is published through the task workspace.",
            excerpt="The chart is resolved through the authenticated Workspace API rather than a storage URL.",
            source_type="compute",
            stance="support",
            relevance=0.94,
            workspace_path=chart_file.workspace_path,
            derived_from=["evidence_phase11_web", "evidence_phase11_inline"],
            execution_ref="phase11_browser_fixture",
            content_hash=chart_file.content_hash,
        ),
    ]

    report = ReportAst(
        report_version=1,
        title="Phase 11 可追溯研究工作台验收报告",
        sections=[
            Section(
                section_id="summary",
                heading="执行摘要",
                level=1,
                blocks=[
                    ParagraphBlock(
                        segments=[
                            TextSegment(text="真实浏览器通过统一 HTTP 与 SSE 客户端读取服务端投影。"),
                            ClaimSegment(
                                claim_id="claim_phase11_authority",
                                text=" 资源缓存不构成第二套 ResearchState。",
                                citation_ids=["cite_phase11_web"],
                            ),
                        ]
                    ),
                    ImageBlock(
                        workspace_path=chart_file.workspace_path,
                        content_hash=chart_file.content_hash,
                        alt_text="证据平衡图",
                        caption="由真实 MinIO Workspace 路径提供的验收图像",
                        citation_ids=["cite_phase11_workspace"],
                    ),
                ],
            ),
            Section(
                section_id="conclusion",
                heading="结论",
                level=2,
                blocks=[
                    ParagraphBlock(
                        segments=[
                            ClaimSegment(
                                claim_id="claim_phase11_renderer",
                                text="结构化 ReportAst 与引用跳转可以独立复核。",
                                citation_ids=["cite_phase11_inline"],
                            )
                        ]
                    )
                ],
            ),
        ],
        citations=[
            Citation(citation_id="cite_phase11_web", evidence_id="evidence_phase11_web", locator="contract"),
            Citation(citation_id="cite_phase11_inline", evidence_id="evidence_phase11_inline", locator="matrix"),
            Citation(citation_id="cite_phase11_workspace", evidence_id="evidence_phase11_workspace", locator="chart"),
        ],
    )
    report_bytes = report.model_dump_json().encode()
    report_file = container.workspace_service.publish_bytes(
        tenant_id=tenant_id,
        task_id=args.task_id,
        path="reports/final/report-v1.json",
        data=report_bytes,
        mime_type="application/json",
        operation_key=report_operation,
    )

    state.route = RouteDecision(mode="deep")
    state.evidence_pool = evidence
    state.report.final = ReportArtifact(
        version=1,
        workspace_path=report_file.workspace_path,
        content_hash=report_file.content_hash,
    )
    state.timestamps.updated_at = utcnow()
    query_hash, plan_hash = _question_hashes(state)

    entity_contract = GraphEntity(
        entity_id="entity_phase11_contract",
        canonical_name="Phase 11 contract",
        entity_type="Concept",
        aliases=["frontend workbench contract"],
    )
    entity_projection = GraphEntity(
        entity_id="entity_server_projection",
        canonical_name="Server projection",
        entity_type="Concept",
        aliases=["authoritative resource DTO"],
    )
    claim = GraphClaim(
        claim_id="claim_phase11_graph",
        claim_key=digest(b"phase11-graph-claim"),
        predicate="uses_authoritative_state",
        subject_entity_id=entity_contract.entity_id,
        object_entity_id=entity_projection.entity_id,
        qualifiers={"scope": "frontend"},
        provenance=[GraphProvenance(evidence_id="evidence_phase11_web", locator="contract")],
    )
    container.knowledge_graph_service.store.clear(tenant_id, args.task_id)
    container.knowledge_graph_service.store.upsert(
        tenant_id,
        args.task_id,
        [entity_contract, entity_projection],
        [claim],
    )

    with container.session_factory() as session, session.begin():
        task = session.get(ResearchTaskRow, args.task_id)
        assert task is not None
        task.status = "completed"
        task.latest_state = state.model_dump(mode="json")
        task.updated_at = utcnow()
        thread = session.get(ThreadRow, thread_id)
        if thread is not None:
            thread.updated_at = task.updated_at
        metadata = session.get(TaskKnowledgeGraphRow, (tenant_id, args.task_id))
        if metadata is None:
            metadata = TaskKnowledgeGraphRow(
                tenant_id=tenant_id,
                task_id=args.task_id,
                status="ready",
                query_hash=query_hash,
                plan_hash=plan_hash,
                processed_evidence_count=len(evidence),
                updated_at=utcnow(),
            )
            session.add(metadata)
        else:
            metadata.status = "ready"
            metadata.query_hash = query_hash
            metadata.plan_hash = plan_hash
            metadata.processed_evidence_count = len(evidence)
            metadata.updated_at = utcnow()
        existing = session.scalar(
            select(ConversationMessageRow).where(
                ConversationMessageRow.task_id == args.task_id,
                ConversationMessageRow.role == "assistant",
                ConversationMessageRow.content.like("Phase 11 fixture:%"),
            )
        )
        if existing is None:
            session.add(
                ConversationMessageRow(
                    message_id=new_id("message"),
                    tenant_id=tenant_id,
                    user_id=args.expected_user_id,
                    thread_id=thread_id,
                    task_id=args.task_id,
                    role="assistant",
                    content="Phase 11 fixture: Evidence、KG、Workspace 与引用报告已发布。",
                    created_at=utcnow(),
                )
            )

    pending_result: dict[str, str] = {}
    if args.pending_task_id:
        with container.session_factory() as session, session.begin():
            pending_task = session.get(ResearchTaskRow, args.pending_task_id)
            if pending_task is None or pending_task.user_id != args.expected_user_id:
                raise SystemExit("Pending-write fixture target is missing or owned by another user.")
            pending_state = ResearchState.model_validate(pending_task.latest_state)
            write_id = f"write_{digest(f'phase11:{args.pending_task_id}:write'.encode())[:32]}"
            plan = {
                "plan_id": "plan_phase11_browser",
                "datasource_id": "datasource_phase11_browser",
                "operation": "update",
                "table": "public.acceptance_items",
                "values": {"reviewed": True},
                "filters": [{"column": "id", "op": "=", "value": 11}],
                "reason": "Verify that a refreshed browser restores the pending approval card.",
                "preconditions": {"reviewed": False},
            }
            plan_hash_value = digest(
                json.dumps(plan, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
            )
            operation_key = digest(f"phase11:{write_id}:operation".encode())
            existing_pending = session.get(
                PendingWriteRow, (pending_task.tenant_id, write_id)
            )
            if existing_pending is None:
                session.add(
                    PendingWriteRow(
                        tenant_id=pending_task.tenant_id,
                        write_id=write_id,
                        task_id=pending_task.task_id,
                        user_id=pending_task.user_id,
                        plan_json=plan,
                        plan_hash=plan_hash_value,
                        preview_json={
                            "datasource": "Phase 11 acceptance database",
                            "operation": "update",
                            "table": "public.acceptance_items",
                            "values": {"reviewed": True},
                            "filters": [{"column": "id", "op": "=", "value": 11}],
                            "reason": plan["reason"],
                            "current_version": {"reviewed": False},
                            "expected_value": False,
                        },
                        policy_revision="phase11-fixture-v1",
                        status="pending",
                        operation_key=operation_key,
                    )
                )
            elif existing_pending.status != "pending":
                raise SystemExit("Pending-write fixture was already decided; use a fresh Task.")
            pending_state.pending_write = write_id
            pending_state.timestamps.updated_at = utcnow()
            pending_task.latest_state = pending_state.model_dump(mode="json")
            pending_task.status = "paused"
            pending_task.lease_token = None
            pending_task.leased_until = None
            pending_task.updated_at = utcnow()
            session.add(
                ResearchCheckpointRow(
                    checkpoint_id=new_id("ckpt"),
                    tenant_id=pending_task.tenant_id,
                    task_id=pending_task.task_id,
                    checkpoint_seq=CheckpointRepository(
                        session, pending_task.tenant_id, pending_task.task_id
                    ).next_seq(),
                    state_snapshot=pending_task.latest_state,
                    graph_position="prepare_write",
                    scheduling_state={"write_id": write_id, "fixture": "phase11"},
                )
            )
            pending_result = {
                "pending_task_id": pending_task.task_id,
                "pending_thread_id": pending_task.thread_id,
            }

    print(
        json.dumps(
            {"task_id": args.task_id, "thread_id": thread_id, **pending_result},
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
