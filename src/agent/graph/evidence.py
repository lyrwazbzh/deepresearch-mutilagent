from __future__ import annotations

from typing import Literal, Protocol

from src.agent.identity import content_hash, evidence_id
from src.agent.schema.state import AdoptedResult, Evidence, ResearchState
from src.agent.backends.workspace import WorkspaceService


class EvidenceConsistencyError(RuntimeError):
    pass


class BranchNotTerminalError(RuntimeError):
    pass


class DocumentEvidenceResolver(Protocol):
    def verify_chunk_hash(
        self, tenant_id: str, doc_id: str, chunk_id: str, content_hash: str
    ) -> bool: ...


class EvidencePipeline:
    def __init__(
        self, workspace: WorkspaceService, documents: DocumentEvidenceResolver
    ) -> None:
        self.workspace = workspace
        self.documents = documents

    def merge(self, state: ResearchState, *, depth: int | None = None) -> list[Evidence]:
        if state.plan is None:
            raise EvidenceConsistencyError("MergeEvidence requires a HypothesisPlan")
        selected_depth = state.execution.depth if depth is None else depth
        round_ = state.research_rounds.get(str(selected_depth))
        if round_ is None:
            raise EvidenceConsistencyError("ResearchRound does not exist")
        terminal = {"completed", "failed", "skipped"}
        branches = {"web": round_.web, "rag": round_.rag, "sql": round_.sql}
        if round_.compute is not None:
            branches["compute"] = round_.compute
        pending = [name for name, branch in branches.items() if branch.status not in terminal]
        if pending:
            raise BranchNotTerminalError(f"Branches are not terminal: {sorted(pending)}")

        hypothesis_ids = {item.hypothesis_id for item in state.plan.hypotheses}
        existing = {item.evidence_id: item for item in state.evidence_pool}
        additions: list[Evidence] = []
        for branch_name, branch in branches.items():
            for adopted in branch.items:
                if adopted.hypothesis_id not in hypothesis_ids:
                    raise EvidenceConsistencyError(
                        f"Unknown hypothesis_id: {adopted.hypothesis_id}"
                    )
                self._verify_location(
                    state, branch_name, adopted  # type: ignore[arg-type]
                )
                identity = evidence_id(
                    state.task_id,
                    adopted.hypothesis_id,
                    branch_name,
                    adopted.result_key,
                )
                evidence = Evidence(
                    evidence_id=identity,
                    hypothesis_id=adopted.hypothesis_id,
                    summary=adopted.summary,
                    excerpt=adopted.excerpt,
                    source_type=branch_name,  # type: ignore[arg-type]
                    stance=adopted.stance,
                    relevance=adopted.relevance,
                    data=adopted.data,
                    doc_id=adopted.doc_id,
                    chunk_id=adopted.chunk_id,
                    workspace_path=adopted.workspace_path,
                    source_uri=adopted.source_uri,
                    published_at=adopted.published_at,
                    retrieved_at=adopted.retrieved_at,
                    content_hash=adopted.content_hash,
                    derived_from=adopted.derived_from,
                    execution_ref=adopted.execution_ref,
                )
                prior = existing.get(identity)
                if prior is not None:
                    if prior.model_dump(mode="json") != evidence.model_dump(mode="json"):
                        raise EvidenceConsistencyError(
                            f"Evidence {identity} replay conflicts with immutable fields"
                        )
                    continue
                existing[identity] = evidence
                additions.append(evidence)
        return [*state.evidence_pool, *additions]

    def _verify_location(
        self,
        state: ResearchState,
        branch: Literal["web", "rag", "sql", "compute"],
        item: AdoptedResult,
    ) -> None:
        if item.data is not None:
            expected = content_hash(item.data)
            if expected != item.content_hash:
                raise EvidenceConsistencyError("Inline adopted result content_hash mismatch")
        elif item.workspace_path is not None:
            metadata = self.workspace.metadata(
                state.tenant_id, state.task_id, item.workspace_path
            )
            if metadata.content_hash != item.content_hash:
                raise EvidenceConsistencyError("Workspace adopted result content_hash mismatch")
        else:
            assert item.doc_id and item.chunk_id
            if not self.documents.verify_chunk_hash(
                state.tenant_id, item.doc_id, item.chunk_id, item.content_hash
            ):
                raise EvidenceConsistencyError("Document adopted result content_hash mismatch")

        if branch == "web":
            if not item.source_uri or not item.retrieved_at:
                raise EvidenceConsistencyError("Web result requires source_uri and retrieved_at")
            if item.doc_id or item.chunk_id:
                raise EvidenceConsistencyError("Web result cannot use a Document location")
        elif branch == "rag":
            if not (item.doc_id and item.chunk_id):
                raise EvidenceConsistencyError("RAG result requires doc_id + chunk_id")
            if item.source_uri is not None:
                raise EvidenceConsistencyError("RAG result does not use source_uri")
        elif branch == "sql":
            if item.doc_id or item.chunk_id or item.source_uri is not None:
                raise EvidenceConsistencyError("SQL result has an invalid source location")
        elif branch == "compute":
            if not (item.workspace_path and item.derived_from and item.execution_ref):
                raise EvidenceConsistencyError(
                    "Compute result requires workspace_path, derived_from and execution_ref"
                )
