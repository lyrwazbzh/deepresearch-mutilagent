from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from pydantic import ValidationError
from sqlalchemy import select

from src.agent.identity import canonical_json, content_hash, sha256_hex
from src.agent.schema.report import (
    CitationValidationIssue,
    CitationValidationResult,
    ClaimSegment,
    ImageBlock,
    ListBlock,
    ParagraphBlock,
    ReportAst,
    TableBlock,
)
from src.agent.schema.state import Evidence, ReportArtifact, ResearchState
from src.agent.backends.persistence.database import SessionFactory
from src.agent.backends.persistence.models import DocumentChunkRow, DocumentRow
from src.agent.backends.observability import Telemetry, TraceScope, add_span_event
from src.agent.errors import NotFoundError
from src.agent.backends.workspace import WorkspaceService


class ReportDependencyError(RuntimeError):
    """A validator dependency failed; this is not a fabricated citation issue."""


def _issue(code: str, message: str, **identity: str | None) -> CitationValidationIssue:
    return CitationValidationIssue(severity="error", code=code, message=message, **identity)


class CitationValidator:
    def __init__(
        self,
        session_factory: SessionFactory,
        workspace: WorkspaceService,
        telemetry: Telemetry | None = None,
    ) -> None:
        self.session_factory = session_factory
        self.workspace = workspace
        self.telemetry = telemetry

    def validate(
        self,
        report: ReportAst | dict[str, Any],
        state: ResearchState,
        *,
        tenant_id: str,
        user_id: str,
        task_id: str,
    ) -> CitationValidationResult:
        if self.telemetry is None:
            return self._validate(
                report,
                state,
                tenant_id=tenant_id,
                user_id=user_id,
                task_id=task_id,
            )
        with self.telemetry.span(
            "citation.validate",
            scope=TraceScope(tenant_id=tenant_id, user_id=user_id, task_id=task_id),
        ):
            result = self._validate(
                report,
                state,
                tenant_id=tenant_id,
                user_id=user_id,
                task_id=task_id,
            )
            for issue in result.issues:
                if issue.severity != "error":
                    continue
                add_span_event(
                    "citation.validation_failed",
                    {
                        "validation.code": issue.code,
                        "claim_id": issue.claim_id,
                        "citation_id": issue.citation_id,
                        "evidence_id": issue.evidence_id,
                    },
                )
            return result

    def _validate(
        self,
        report: ReportAst | dict[str, Any],
        state: ResearchState,
        *,
        tenant_id: str,
        user_id: str,
        task_id: str,
    ) -> CitationValidationResult:
        del user_id
        try:
            ast = report if isinstance(report, ReportAst) else ReportAst.model_validate(report)
        except ValidationError as exc:
            return CitationValidationResult(
                valid=False,
                issues=[_issue("invalid_report_structure", str(exc)[:1000])],
            )
        issues: list[CitationValidationIssue] = []
        citation_ids = [item.citation_id for item in ast.citations]
        duplicate_citations = sorted({item for item in citation_ids if citation_ids.count(item) > 1})
        for citation_id in duplicate_citations:
            issues.append(_issue("duplicate_citation_id", "Citation ID is duplicated.", citation_id=citation_id))
        claims: list[ClaimSegment] = []
        referenced: list[tuple[str | None, str]] = []
        for section in ast.sections:
            for block in section.blocks:
                if isinstance(block, ParagraphBlock):
                    claims.extend(item for item in block.segments if isinstance(item, ClaimSegment))
                elif isinstance(block, ListBlock):
                    claims.extend(
                        item
                        for list_item in block.items
                        for item in list_item.segments
                        if isinstance(item, ClaimSegment)
                    )
                elif isinstance(block, TableBlock):
                    if block.rows and not block.citation_ids:
                        issues.append(_issue("missing_citation", "Data table requires a citation."))
                    referenced.extend((None, item) for item in block.citation_ids)
                elif isinstance(block, ImageBlock):
                    issues.extend(
                        self._validate_image(block, ast, state, tenant_id=tenant_id, task_id=task_id)
                    )
                    referenced.extend((None, item) for item in block.citation_ids)
        claim_ids = [item.claim_id for item in claims]
        for claim_id in sorted({item for item in claim_ids if claim_ids.count(item) > 1}):
            issues.append(_issue("duplicate_claim_id", "Claim ID is duplicated.", claim_id=claim_id))
        for claim in claims:
            if not claim.citation_ids:
                issues.append(_issue("missing_citation", "Claim requires at least one citation.", claim_id=claim.claim_id))
            referenced.extend((claim.claim_id, item) for item in claim.citation_ids)
        citation_by_id = {item.citation_id: item for item in ast.citations}
        evidence_by_id = {item.evidence_id: item for item in state.evidence_pool}
        used: set[str] = set()
        checked_evidence: set[str] = set()
        for referenced_claim_id, citation_id in referenced:
            citation = citation_by_id.get(citation_id)
            if citation is None:
                issues.append(
                    _issue("citation_not_found", "Referenced citation does not exist.", claim_id=referenced_claim_id, citation_id=citation_id)
                )
                continue
            used.add(citation_id)
            evidence = evidence_by_id.get(citation.evidence_id)
            if evidence is None:
                issues.append(
                    _issue("evidence_not_found", "Citation evidence does not exist.", claim_id=referenced_claim_id, citation_id=citation_id, evidence_id=citation.evidence_id)
                )
                continue
            if evidence.evidence_id not in checked_evidence:
                checked_evidence.add(evidence.evidence_id)
                source_issue = self._validate_source(evidence, tenant_id=tenant_id, task_id=task_id)
                if source_issue:
                    issues.append(source_issue)
        for citation in ast.citations:
            if citation.citation_id not in used:
                issues.append(
                    CitationValidationIssue(
                        severity="warning", code="unused_citation", message="Citation is not referenced by any report block.",
                        citation_id=citation.citation_id, evidence_id=citation.evidence_id,
                    )
                )
        return CitationValidationResult(
            valid=not any(item.severity == "error" for item in issues), issues=issues
        )

    def _validate_source(
        self, evidence: Evidence, *, tenant_id: str, task_id: str
    ) -> CitationValidationIssue | None:
        try:
            if evidence.data is not None:
                if content_hash(evidence.data) != evidence.content_hash:
                    return _issue("content_hash_mismatch", "Inline Evidence hash does not match.", evidence_id=evidence.evidence_id)
                return None
            if evidence.workspace_path:
                metadata = self.workspace.metadata(tenant_id, task_id, evidence.workspace_path)
                if metadata.content_hash != evidence.content_hash:
                    return _issue("content_hash_mismatch", "Workspace Evidence hash does not match.", evidence_id=evidence.evidence_id)
                return None
            with self.session_factory() as session:
                document = session.scalar(
                    select(DocumentRow).where(
                        DocumentRow.tenant_id == tenant_id,
                        DocumentRow.doc_id == evidence.doc_id,
                        DocumentRow.deleted_at.is_(None),
                    )
                )
                chunk = session.scalar(
                    select(DocumentChunkRow).where(
                        DocumentChunkRow.tenant_id == tenant_id,
                        DocumentChunkRow.doc_id == evidence.doc_id,
                        DocumentChunkRow.chunk_id == evidence.chunk_id,
                    )
                )
            if document is None or chunk is None:
                return _issue("source_not_found", "Document source cannot be resolved.", evidence_id=evidence.evidence_id)
            if chunk.chunk_hash != evidence.content_hash:
                return _issue("content_hash_mismatch", "Document chunk hash does not match.", evidence_id=evidence.evidence_id)
            return None
        except (FileNotFoundError, NotFoundError):
            return _issue("source_not_found", "Evidence source cannot be resolved.", evidence_id=evidence.evidence_id)
        except PermissionError:
            return _issue("permission_denied", "Evidence source is no longer readable.", evidence_id=evidence.evidence_id)
        except (ConnectionError, TimeoutError) as exc:
            raise ReportDependencyError("Citation source dependency is unavailable") from exc

    def _validate_image(
        self,
        image: ImageBlock,
        report: ReportAst,
        state: ResearchState,
        *,
        tenant_id: str,
        task_id: str,
    ) -> list[CitationValidationIssue]:
        try:
            metadata = self.workspace.metadata(tenant_id, task_id, image.workspace_path)
        except (FileNotFoundError, NotFoundError):
            return [_issue("source_not_found", "Image source cannot be resolved.")]
        if metadata.content_hash != image.content_hash:
            return [_issue("content_hash_mismatch", "Image hash does not match Workspace.")]
        if not metadata.producer_tool_call_id:
            return []
        if not image.citation_ids:
            return [_issue("missing_citation", "Data image requires upstream Evidence citations.")]
        try:
            raw = self.workspace.read(
                tenant_id, task_id, f"compute/{metadata.producer_tool_call_id}/manifest.json"
            )
            manifest = json.loads(raw)
        except (FileNotFoundError, NotFoundError):
            return [_issue("source_not_found", "Compute lineage manifest is missing.")]
        output = next(
            (item for item in manifest.get("outputs", []) if item.get("workspace_path") == image.workspace_path),
            None,
        )
        if output is None or output.get("content_hash") != image.content_hash:
            return [_issue("content_hash_mismatch", "Compute manifest does not match image output.")]
        citations = {item.citation_id: item.evidence_id for item in report.citations}
        cited_evidence = {citations[item] for item in image.citation_ids if item in citations}
        required = {str(item["evidence_id"]) for item in manifest.get("input_evidence", [])}
        if not required <= cited_evidence:
            return [_issue("missing_citation", "Image citations do not cover compute input Evidence.")]
        known = {item.evidence_id for item in state.evidence_pool}
        if not required <= known:
            return [_issue("evidence_not_found", "Compute manifest references missing Evidence.")]
        return []


def _escape(value: str) -> str:
    return re.sub(r"([\\`*_{}\[\]()#+.!|>-])", r"\\\1", value)


class MarkdownRenderer:
    def render(self, report: ReportAst, evidence_pool: list[Evidence]) -> bytes:
        evidence = {item.evidence_id: item for item in evidence_pool}
        lines = [f"# {_escape(report.title)}", ""]

        def render_segments(segments: list[Any]) -> str:
            values: list[str] = []
            for segment in segments:
                values.append(_escape(segment.text))
                if isinstance(segment, ClaimSegment):
                    values.extend(f"[^cite-{item}]" for item in segment.citation_ids)
            return "".join(values)

        for section in report.sections:
            lines.extend([f"{'#' * section.level} {_escape(section.heading)}", ""])
            for block in section.blocks:
                if isinstance(block, ParagraphBlock):
                    lines.extend([render_segments(block.segments), ""])
                elif isinstance(block, ListBlock):
                    for index, item in enumerate(block.items, 1):
                        marker = f"{index}." if block.ordered else "-"
                        lines.append(f"{marker} {render_segments(item.segments)}")
                    lines.append("")
                elif isinstance(block, TableBlock):
                    if block.caption:
                        lines.extend([f"*{_escape(block.caption)}*", ""])
                    lines.append("| " + " | ".join(_escape(item) for item in block.columns) + " |")
                    lines.append("| " + " | ".join("---" for _ in block.columns) + " |")
                    lines.extend("| " + " | ".join(_escape(item) for item in row) + " |" for row in block.rows)
                    if block.citation_ids:
                        lines.append("".join(f"[^cite-{item}]" for item in block.citation_ids))
                    lines.append("")
                elif isinstance(block, ImageBlock):
                    lines.append(f"![{_escape(block.alt_text)}]({block.workspace_path})")
                    if block.caption:
                        lines.append(_escape(block.caption))
                    if block.citation_ids:
                        lines.append("".join(f"[^cite-{item}]" for item in block.citation_ids))
                    lines.append("")
        for citation in report.citations:
            evidence_item = evidence.get(citation.evidence_id)
            title = evidence_item.summary if evidence_item else citation.evidence_id
            if evidence_item and evidence_item.source_uri:
                source = f"[{_escape(title)}]({evidence_item.source_uri})"
            else:
                source = _escape(title)
            locator = f", {_escape(citation.locator)}" if citation.locator else ""
            lines.append(f"[^cite-{citation.citation_id}]: {source}{locator}")
        return ("\n".join(lines).rstrip() + "\n").encode("utf-8")


@dataclass(frozen=True)
class PublishedReport:
    ast: ReportAst
    artifact: ReportArtifact


class ReportPublisher:
    def __init__(self, workspace: WorkspaceService) -> None:
        self.workspace = workspace
        self.renderer = MarkdownRenderer()

    def publish(
        self, *, tenant_id: str, task_id: str, report: ReportAst, state: ResearchState
    ) -> PublishedReport:
        version = max(
            report.report_version,
            (state.report.draft.version + 1) if state.report.draft else 1,
            (state.report.final.version + 1) if state.report.final else 1,
        )
        json_path = f"reports/versions/{version}/report.json"
        md_path = f"reports/versions/{version}/report.md"
        try:
            existing = self.workspace.read(tenant_id, task_id, json_path)
            ast = ReportAst.model_validate_json(existing)
        except (FileNotFoundError, NotFoundError):
            ast = report.model_copy(update={"report_version": version})
            existing = canonical_json(ast.model_dump(mode="json"))
        json_file = self.workspace.publish_bytes(
            tenant_id=tenant_id, task_id=task_id, path=json_path, data=existing,
            mime_type="application/json", operation_key=sha256_hex(f"report:{version}:json"),
        )
        markdown = self.renderer.render(ast, state.evidence_pool)
        self.workspace.publish_bytes(
            tenant_id=tenant_id, task_id=task_id, path=md_path, data=markdown,
            mime_type="text/markdown", operation_key=sha256_hex(f"report:{version}:markdown"),
        )
        return PublishedReport(
            ast=ast,
            artifact=ReportArtifact(
                version=version, workspace_path=json_file.workspace_path,
                content_hash=json_file.content_hash,
            ),
        )
