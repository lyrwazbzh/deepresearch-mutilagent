from __future__ import annotations

import json
import uuid
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import delete

from src.agent.tools.documents import RetrievalGateway
from src.agent.tools.skill_management import SkillRegistry, SkillRuntime
from src.agent.identity import canonical_json, sha256_hex
from src.agent.memory.domain import MemoryCandidate
from src.agent.schema.model_config import FrozenModelBinding
from src.agent.schema.report import ClaimSegment, ListBlock, ParagraphBlock, ReportAst
from src.agent.schema.state import ResearchState
from src.agent.llm.runtime import ChatModel, ModelCallError
from src.agent.memory.service import MemoryService
from src.agent.backends.persistence.database import SessionFactory
from src.agent.backends.persistence.models import (
    EvalCaseResultRow,
    EvalRunRow,
    ResearchMemoryRow,
    utcnow,
)
from src.agent.reporting.reporting import CitationValidator


EvalSuiteName = Literal["rag", "research_ltm", "e2e", "skill"]
CaseOutcome = Literal["passed", "failed", "error", "judge_error"]


@dataclass(frozen=True)
class ImmutableDataset:
    suite: EvalSuiteName
    version: str
    cases: tuple[Mapping[str, Any], ...]
    metadata: Mapping[str, Any] = field(default_factory=dict)

    @property
    def dataset_ref(self) -> str:
        payload = {
            "suite": self.suite,
            "version": self.version,
            "cases": [dict(item) for item in self.cases],
            "metadata": dict(self.metadata),
        }
        return f"sha256:{sha256_hex(canonical_json(payload))}"

    def validate(self) -> None:
        case_ids = [str(item.get("case_id") or "") for item in self.cases]
        if not case_ids or any(not item for item in case_ids):
            raise ValueError("Evaluation Dataset requires stable, non-empty case_id values")
        if len(case_ids) != len(set(case_ids)):
            raise ValueError("Evaluation Dataset case_id values must be unique")


@dataclass
class ResourceManifest:
    documents: list[str] = field(default_factory=list)
    tasks: list[str] = field(default_factory=list)
    workspace_paths: list[str] = field(default_factory=list)
    memory_ids: list[str] = field(default_factory=list)
    es_generations: list[str] = field(default_factory=list)
    kg_namespaces: list[str] = field(default_factory=list)

    def add(self, kind: str, value: str) -> None:
        target = getattr(self, kind, None)
        if not isinstance(target, list):
            raise ValueError(f"Unknown manifest resource kind: {kind}")
        if value not in target:
            target.append(value)

    def snapshot(self) -> dict[str, list[str]]:
        return {
            key: list(getattr(self, key))
            for key in (
                "documents",
                "tasks",
                "workspace_paths",
                "memory_ids",
                "es_generations",
                "kg_namespaces",
            )
        }

    @classmethod
    def from_snapshot(cls, value: Mapping[str, Any]) -> "ResourceManifest":
        return cls(
            documents=list(value.get("documents") or []),
            tasks=list(value.get("tasks") or []),
            workspace_paths=list(value.get("workspace_paths") or []),
            memory_ids=list(value.get("memory_ids") or []),
            es_generations=list(value.get("es_generations") or []),
            kg_namespaces=list(value.get("kg_namespaces") or []),
        )


@dataclass(frozen=True)
class EvalCaseResult:
    case_id: str
    outcome: CaseOutcome
    metrics: dict[str, Any] = field(default_factory=dict)
    diagnostics: dict[str, Any] = field(default_factory=dict)


class EvaluationSuite(Protocol):
    name: EvalSuiteName

    def preflight(self, dataset: ImmutableDataset, config: Mapping[str, Any]) -> None: ...

    def setup(
        self,
        dataset: ImmutableDataset,
        eval_scope_id: str,
        manifest: ResourceManifest,
    ) -> None: ...

    def execute_case(
        self,
        case: Mapping[str, Any],
        eval_scope_id: str,
        manifest: ResourceManifest,
    ) -> EvalCaseResult: ...

    def summarize(self, results: Sequence[EvalCaseResult]) -> dict[str, Any]: ...

    def cleanup(self, eval_scope_id: str, manifest: ResourceManifest) -> None: ...


class EvalRunner:
    """Persistent runner; Suite setup/cases use the injected production services."""

    def __init__(
        self,
        session_factory: SessionFactory,
        *,
        id_factory: Callable[[str], str] | None = None,
        now: Callable[[], datetime] = utcnow,
    ) -> None:
        self.session_factory = session_factory
        self.id_factory = id_factory or (lambda prefix: f"{prefix}_{uuid.uuid4().hex}")
        self.now = now

    def run(
        self,
        suite: EvaluationSuite,
        dataset: ImmutableDataset,
        *,
        config_snapshot: Mapping[str, Any],
        code_revision: str,
    ) -> EvalRunRow:
        dataset.validate()
        if dataset.suite != suite.name:
            raise ValueError("Dataset suite does not match Evaluation Suite")
        resolved_config = json.loads(canonical_json(dict(config_snapshot)))
        suite.preflight(dataset, resolved_config)
        eval_run_id = self.id_factory("eval")
        eval_scope_id = self.id_factory("evalscope")
        manifest = ResourceManifest()
        with self.session_factory() as session, session.begin():
            session.add(
                EvalRunRow(
                    eval_run_id=eval_run_id,
                    suite=suite.name,
                    dataset_ref=dataset.dataset_ref,
                    config_snapshot=resolved_config,
                    code_revision=code_revision,
                    eval_scope_id=eval_scope_id,
                    status="running",
                    metrics_summary={"resource_manifest": manifest.snapshot()},
                    created_at=self.now(),
                )
            )

        results: list[EvalCaseResult] = []
        run_failure: BaseException | None = None
        cleanup_failure: BaseException | None = None
        try:
            suite.setup(dataset, eval_scope_id, manifest)
            self._save_manifest(eval_run_id, manifest)
            for case in dataset.cases:
                case_id = str(case["case_id"])
                try:
                    result = suite.execute_case(case, eval_scope_id, manifest)
                    if result.case_id != case_id:
                        raise ValueError("Suite returned a result for a different case_id")
                except JudgeError as exc:
                    result = EvalCaseResult(
                        case_id=case_id,
                        outcome="judge_error",
                        metrics=exc.null_metrics,
                        diagnostics={"error.type": "judge_error", "reason_code": exc.reason_code},
                    )
                except Exception as exc:
                    result = EvalCaseResult(
                        case_id=case_id,
                        outcome="error",
                        diagnostics={"error.type": type(exc).__name__},
                    )
                self._save_case(eval_run_id, result)
                results.append(result)
                self._save_manifest(eval_run_id, manifest)
        except BaseException as exc:
            run_failure = exc
        finally:
            try:
                suite.cleanup(eval_scope_id, manifest)
            except BaseException as exc:
                cleanup_failure = exc

        failed = run_failure is not None or cleanup_failure is not None
        summary = suite.summarize(results) if results else {}
        judge_errors = sum(item.outcome == "judge_error" for item in results)
        summary.update(
            {
                "judge_error_count": judge_errors,
                "gate_eligible": not failed and judge_errors == 0,
                "resource_manifest": manifest.snapshot(),
            }
        )
        if run_failure is not None:
            summary["run_error"] = type(run_failure).__name__
        if cleanup_failure is not None:
            summary["cleanup_error"] = type(cleanup_failure).__name__
        with self.session_factory() as session, session.begin():
            row = session.get(EvalRunRow, eval_run_id)
            assert row is not None
            row.status = "failed" if failed else "completed"
            row.metrics_summary = summary
            row.completed_at = self.now()
        with self.session_factory() as session:
            persisted = session.get(EvalRunRow, eval_run_id)
            assert persisted is not None
            session.expunge(persisted)
        if run_failure is not None:
            raise run_failure
        if cleanup_failure is not None:
            raise cleanup_failure
        return persisted

    def _save_case(self, eval_run_id: str, result: EvalCaseResult) -> None:
        with self.session_factory() as session, session.begin():
            session.add(
                EvalCaseResultRow(
                    eval_run_id=eval_run_id,
                    case_id=result.case_id,
                    outcome=result.outcome,
                    metrics=result.metrics,
                    diagnostics=result.diagnostics,
                )
            )

    def _save_manifest(self, eval_run_id: str, manifest: ResourceManifest) -> None:
        with self.session_factory() as session, session.begin():
            row = session.get(EvalRunRow, eval_run_id)
            if row is not None:
                row.metrics_summary = {"resource_manifest": manifest.snapshot()}

    def recover_cleanup(self, suite: EvaluationSuite, eval_scope_id: str) -> None:
        """Clean an interrupted run strictly from its persisted manifest."""
        with self.session_factory() as session:
            row = session.query(EvalRunRow).filter_by(eval_scope_id=eval_scope_id).one_or_none()
            if row is None:
                raise LookupError(f"Unknown eval_scope_id: {eval_scope_id}")
            summary = dict(row.metrics_summary or {})
        manifest = ResourceManifest.from_snapshot(
            dict(summary.get("resource_manifest") or {})
        )
        suite.cleanup(eval_scope_id, manifest)


class ResourceManifestCleaner:
    """Composable production lifecycle adapter; deletes only registered IDs."""

    def __init__(
        self,
        handlers: Mapping[str, Callable[[str], None]],
    ) -> None:
        self.handlers = dict(handlers)

    def __call__(self, eval_scope_id: str, manifest: ResourceManifest) -> None:
        del eval_scope_id
        for kind in (
            "kg_namespaces",
            "es_generations",
            "workspace_paths",
            "documents",
            "tasks",
            "memory_ids",
        ):
            handler = self.handlers.get(kind)
            if handler is None:
                continue
            for resource_id in reversed(list(getattr(manifest, kind))):
                handler(resource_id)


def recall_at_k(returned: Sequence[str], relevant: Sequence[str], k: int = 5) -> float:
    gold = set(relevant)
    if not gold:
        raise ValueError("Recall requires at least one relevant ID")
    return len(set(returned[:k]) & gold) / len(gold)


def precision_recall_f1(tp: int, fp: int, fn: int) -> tuple[float, float, float]:
    precision = tp / (tp + fp) if tp + fp else 0.0
    recall = tp / (tp + fn) if tp + fn else 0.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    return precision, recall, f1


class BaseSuite:
    name: EvalSuiteName

    def __init__(
        self,
        *,
        preflight: Callable[[], None] | None = None,
        setup: Callable[[ImmutableDataset, str, ResourceManifest], None] | None = None,
        cleanup: Callable[[str, ResourceManifest], None] | None = None,
    ) -> None:
        self._preflight = preflight
        self._setup = setup
        self._cleanup = cleanup

    def preflight(self, dataset: ImmutableDataset, config: Mapping[str, Any]) -> None:
        del dataset, config
        if self._preflight:
            self._preflight()

    def setup(
        self, dataset: ImmutableDataset, eval_scope_id: str, manifest: ResourceManifest
    ) -> None:
        if self._setup:
            self._setup(dataset, eval_scope_id, manifest)

    def cleanup(self, eval_scope_id: str, manifest: ResourceManifest) -> None:
        if self._cleanup:
            self._cleanup(eval_scope_id, manifest)


class RAGEvalSuite(BaseSuite):
    name: EvalSuiteName = "rag"

    def __init__(
        self,
        retrieval: RetrievalGateway,
        embedding_binding: FrozenModelBinding,
        rerank_binding: FrozenModelBinding,
        *,
        retrieval_config: Literal["bm25_baseline", "current"] = "current",
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self.retrieval = retrieval
        self.embedding_binding = embedding_binding
        self.rerank_binding = rerank_binding
        self.retrieval_config = retrieval_config

    def preflight(self, dataset: ImmutableDataset, config: Mapping[str, Any]) -> None:
        super().preflight(dataset, config)
        for case in dataset.cases:
            if not case.get("relevant_chunk_ids"):
                raise ValueError("RAG Eval relevant_chunk_ids must not be empty")

    def execute_case(
        self,
        case: Mapping[str, Any],
        eval_scope_id: str,
        manifest: ResourceManifest,
    ) -> EvalCaseResult:
        del eval_scope_id, manifest
        tenant_id = str(case["tenant_id"])
        query = str(case["query"])
        doc_ids = list(case.get("doc_ids") or []) or None
        tags = list(case.get("tags") or []) or None
        if self.retrieval_config == "bm25_baseline":
            hits = self.retrieval.search_bm25_baseline(
                tenant_id=tenant_id,
                query=query,
                doc_ids=doc_ids,
                tags=tags,
                top_k=5,
            )
            degraded: list[str] = []
        else:
            hits, degraded = self.retrieval.search(
                tenant_id=tenant_id,
                query=query,
                doc_ids=doc_ids,
                tags=tags,
                top_k=5,
                embedding_binding=self.embedding_binding,
                rerank_binding=self.rerank_binding,
            )
        returned = [item.chunk.chunk_id for item in hits[:5]]
        value = recall_at_k(returned, list(case["relevant_chunk_ids"]), 5)
        return EvalCaseResult(
            case_id=str(case["case_id"]),
            outcome="passed",
            metrics={"recall_at_5": value},
            diagnostics={
                "retrieval_config": self.retrieval_config,
                "returned_chunk_ids": returned,
                "degraded_channels": degraded,
            },
        )

    def summarize(self, results: Sequence[EvalCaseResult]) -> dict[str, Any]:
        values = [float(item.metrics["recall_at_5"]) for item in results if "recall_at_5" in item.metrics]
        return {
            "recall_at_5": sum(values) / len(values) if values else None,
            "retrieval_config": self.retrieval_config,
        }


class ResearchLTMEvalSuite(BaseSuite):
    name: EvalSuiteName = "research_ltm"

    def __init__(
        self,
        memory: MemoryService,
        session_factory: SessionFactory,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self.memory = memory
        self.session_factory = session_factory
        self.logical_memories: dict[str, str] = {}

    def execute_case(
        self,
        case: Mapping[str, Any],
        eval_scope_id: str,
        manifest: ResourceManifest,
    ) -> EvalCaseResult:
        kind = str(case["case_kind"])
        tenant_id = str(case["tenant_id"])
        user_id = str(case["user_id"])
        if kind == "write_gate":
            candidate = MemoryCandidate.model_validate(case["candidate"])
            results = self.memory.extract_batch(
                tenant_id=tenant_id,
                user_id=user_id,
                candidates=[candidate],
                user_messages=dict(case["user_messages"]),
                input_hash=sha256_hex(f"{eval_scope_id}:{case['case_id']}"),
            )
            predicted = bool(results)
            for index, item in enumerate(results):
                manifest.add("memory_ids", item.memory.memory_id)
                logical_id = str(case.get("logical_memory_id") or f"{case['case_id']}:{index}")
                self.logical_memories[logical_id] = item.memory.memory_id
            should_write = bool(case["should_write"])
            return EvalCaseResult(
                case_id=str(case["case_id"]),
                outcome="passed" if predicted == should_write else "failed",
                metrics={"predicted_write": predicted, "should_write": should_write},
            )
        if kind == "recall":
            context = self.memory.recall(
                tenant_id=tenant_id,
                user_id=user_id,
                planning_query=str(case["query"]),
                query_embedding=list(case.get("query_embedding") or []) or None,
            )
            returned = [item.memory_id for item in context.items[:5]]
            relevant = [
                self.logical_memories.get(str(item), str(item))
                for item in case["relevant_memory_ids"]
            ]
            value = recall_at_k(returned, relevant, 5)
            leakage = 0
            if case.get("leakage_tenant_id") and case.get("leakage_user_id"):
                foreign = self.memory.recall(
                    tenant_id=str(case["leakage_tenant_id"]),
                    user_id=str(case["leakage_user_id"]),
                    planning_query=str(case["query"]),
                    query_embedding=list(case.get("query_embedding") or []) or None,
                )
                leakage = len(set(item.memory_id for item in foreign.items) & set(relevant))
            return EvalCaseResult(
                case_id=str(case["case_id"]),
                outcome="passed" if leakage == 0 else "failed",
                metrics={"recall_at_5": value, "leakage_count": leakage},
                diagnostics={"returned_memory_ids": returned},
            )
        raise ValueError(f"Unknown Research LTM case_kind: {kind}")

    def summarize(self, results: Sequence[EvalCaseResult]) -> dict[str, Any]:
        write = [item for item in results if "predicted_write" in item.metrics]
        tp = sum(bool(item.metrics["predicted_write"]) and bool(item.metrics["should_write"]) for item in write)
        fp = sum(bool(item.metrics["predicted_write"]) and not bool(item.metrics["should_write"]) for item in write)
        fn = sum(not bool(item.metrics["predicted_write"]) and bool(item.metrics["should_write"]) for item in write)
        precision, recall, f1 = precision_recall_f1(tp, fp, fn)
        recalls = [float(item.metrics["recall_at_5"]) for item in results if "recall_at_5" in item.metrics]
        return {
            "write_gate_precision": precision,
            "write_gate_recall": recall,
            "write_gate_f1": f1,
            "recall_at_5": sum(recalls) / len(recalls) if recalls else None,
            "cross_tenant_user_leakage": sum(
                int(item.metrics.get("leakage_count", 0)) for item in results
            ),
        }

    def cleanup(self, eval_scope_id: str, manifest: ResourceManifest) -> None:
        try:
            if manifest.memory_ids:
                with self.session_factory() as session, session.begin():
                    session.execute(
                        delete(ResearchMemoryRow).where(
                            ResearchMemoryRow.memory_id.in_(manifest.memory_ids)
                        )
                    )
        finally:
            super().cleanup(eval_scope_id, manifest)


class FactualVerdict(BaseModel):
    model_config = ConfigDict(extra="forbid")
    claim_id: str
    verdict: Literal["correct", "incorrect", "not_verifiable"]
    matched_gold_fact_ids: list[str] = Field(default_factory=list)
    reason: str = Field(max_length=1000)


class FactualJudgeResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    verdicts: list[FactualVerdict]


class FaithfulnessVerdict(BaseModel):
    model_config = ConfigDict(extra="forbid")
    claim_id: str
    supported: bool
    supporting_citation_ids: list[str] = Field(default_factory=list)
    reason: str = Field(max_length=1000)


class FaithfulnessJudgeResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    verdicts: list[FaithfulnessVerdict]


class JudgeError(RuntimeError):
    def __init__(self, reason_code: str, null_metrics: Iterable[str]) -> None:
        super().__init__(reason_code)
        self.reason_code = reason_code
        self.null_metrics: dict[str, float | None] = {
            name: None for name in null_metrics
        }


class FactualJudge(Protocol):
    def evaluate(
        self,
        query: str,
        gold_facts: Sequence[Mapping[str, Any]],
        claims: Sequence[ClaimSegment],
    ) -> list[FactualVerdict]: ...


class FaithfulnessJudge(Protocol):
    def evaluate(
        self,
        claims: Sequence[ClaimSegment],
        evidence_by_citation: Mapping[str, Mapping[str, Any]],
    ) -> list[FaithfulnessVerdict]: ...


class ModelFactualJudge:
    def __init__(
        self,
        model: ChatModel,
        *,
        prompt_version: str,
        rubric_version: str,
        batch_size: int = 20,
    ) -> None:
        self.model = model
        self.prompt_version = prompt_version
        self.rubric_version = rubric_version
        self.batch_size = min(max(batch_size, 1), 50)

    def evaluate(
        self,
        query: str,
        gold_facts: Sequence[Mapping[str, Any]],
        claims: Sequence[ClaimSegment],
    ) -> list[FactualVerdict]:
        output: list[FactualVerdict] = []
        for start in range(0, len(claims), self.batch_size):
            batch = claims[start : start + self.batch_size]
            prompt = canonical_json(
                {
                    "contract": "factual_judge",
                    "prompt_version": self.prompt_version,
                    "rubric_version": self.rubric_version,
                    "query": query,
                    "gold_facts": list(gold_facts),
                    "claims": [item.model_dump(mode="json") for item in batch],
                }
            )
            try:
                response = self.model.invoke_structured(
                    prompt.decode("utf-8"), FactualJudgeResponse
                )
            except (ModelCallError, ValueError) as exc:
                raise JudgeError("factual_output_invalid", ("factual_accuracy",)) from exc
            if {item.claim_id for item in response.verdicts} != {
                item.claim_id for item in batch
            }:
                raise JudgeError("factual_claim_set_mismatch", ("factual_accuracy",))
            output.extend(response.verdicts)
        return output


class ModelFaithfulnessJudge:
    def __init__(
        self,
        model: ChatModel,
        *,
        prompt_version: str,
        rubric_version: str,
        batch_size: int = 20,
    ) -> None:
        self.model = model
        self.prompt_version = prompt_version
        self.rubric_version = rubric_version
        self.batch_size = min(max(batch_size, 1), 50)

    def evaluate(
        self,
        claims: Sequence[ClaimSegment],
        evidence_by_citation: Mapping[str, Mapping[str, Any]],
    ) -> list[FaithfulnessVerdict]:
        output: list[FaithfulnessVerdict] = []
        for start in range(0, len(claims), self.batch_size):
            batch = claims[start : start + self.batch_size]
            prompt = canonical_json(
                {
                    "contract": "faithfulness_judge",
                    "prompt_version": self.prompt_version,
                    "rubric_version": self.rubric_version,
                    "claims": [item.model_dump(mode="json") for item in batch],
                    "resolved_evidence": {
                        citation_id: dict(evidence_by_citation[citation_id])
                        for claim in batch
                        for citation_id in claim.citation_ids
                        if citation_id in evidence_by_citation
                    },
                }
            )
            try:
                response = self.model.invoke_structured(
                    prompt.decode("utf-8"), FaithfulnessJudgeResponse
                )
            except (ModelCallError, ValueError) as exc:
                raise JudgeError("faithfulness_output_invalid", ("faithfulness",)) from exc
            if {item.claim_id for item in response.verdicts} != {
                item.claim_id for item in batch
            }:
                raise JudgeError("faithfulness_claim_set_mismatch", ("faithfulness",))
            output.extend(response.verdicts)
        return output


@dataclass(frozen=True)
class E2ECaseOutput:
    task_status: str
    report: ReportAst | None
    state: ResearchState | None
    evidence_by_citation: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    diagnostics: Mapping[str, Any] = field(default_factory=dict)


class E2EResearchEvalSuite(BaseSuite):
    name: EvalSuiteName = "e2e"

    def __init__(
        self,
        execute_graph_case: Callable[[Mapping[str, Any], str, ResourceManifest], E2ECaseOutput],
        citation_validator: CitationValidator,
        factual_judge: FactualJudge,
        faithfulness_judge: FaithfulnessJudge,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self.execute_graph_case = execute_graph_case
        self.citation_validator = citation_validator
        self.factual_judge = factual_judge
        self.faithfulness_judge = faithfulness_judge

    def execute_case(
        self,
        case: Mapping[str, Any],
        eval_scope_id: str,
        manifest: ResourceManifest,
    ) -> EvalCaseResult:
        output = self.execute_graph_case(case, eval_scope_id, manifest)
        case_kind = str(case["case_kind"])
        if case_kind != "report_quality":
            expected = dict(case.get("expected_diagnostics") or {})
            passed = all(output.diagnostics.get(key) == value for key, value in expected.items())
            return EvalCaseResult(
                case_id=str(case["case_id"]),
                outcome="passed" if passed else "failed",
                diagnostics={"case_kind": case_kind, **dict(output.diagnostics)},
            )
        if output.report is None or output.state is None:
            return EvalCaseResult(
                case_id=str(case["case_id"]),
                outcome="failed",
                metrics={
                    "factual_accuracy": 0.0,
                    "citation_coverage": 0.0,
                    "completion_rate": 0.0,
                    "faithfulness": 0.0,
                },
                diagnostics={"error.category": output.diagnostics.get("error.category")},
            )
        claims = report_claims(output.report)
        validation = self.citation_validator.validate(
            output.report,
            output.state,
            tenant_id=str(case["tenant_id"]),
            user_id=str(case["user_id"]),
            task_id=str(output.diagnostics.get("task_id") or case.get("task_id") or ""),
        )
        coverage = citation_coverage(output.report, output.state, validation.issues)
        completion = float(
            output.task_status == "completed" and output.report is not None and validation.valid
        )
        try:
            factual = self.factual_judge.evaluate(
                str(case["query"]), list(case.get("gold_facts") or []), claims
            )
            faithful = self.faithfulness_judge.evaluate(
                claims, output.evidence_by_citation
            )
        except JudgeError as exc:
            exc.null_metrics.update(
                {
                    "factual_accuracy": None,
                    "faithfulness": None,
                    "citation_coverage": coverage,
                    "completion_rate": completion,
                }
            )
            raise
        factual_accuracy = (
            sum(item.verdict == "correct" for item in factual) / len(factual)
            if factual
            else 0.0
        )
        faithfulness = (
            sum(item.supported for item in faithful) / len(claims) if claims else 0.0
        )
        return EvalCaseResult(
            case_id=str(case["case_id"]),
            outcome="passed" if completion else "failed",
            metrics={
                "factual_accuracy": factual_accuracy,
                "citation_coverage": coverage,
                "completion_rate": completion,
                "faithfulness": faithfulness,
            },
            diagnostics={
                "case_kind": case_kind,
                "factual_verdicts": [item.model_dump(mode="json") for item in factual],
                "faithfulness_verdicts": [item.model_dump(mode="json") for item in faithful],
                "citation_issue_codes": [item.code for item in validation.issues],
            },
        )

    def summarize(self, results: Sequence[EvalCaseResult]) -> dict[str, Any]:
        report_results = [
            item for item in results if item.diagnostics.get("case_kind") == "report_quality"
            or "completion_rate" in item.metrics
        ]
        summary: dict[str, Any] = {}
        for metric in (
            "factual_accuracy",
            "citation_coverage",
            "completion_rate",
            "faithfulness",
        ):
            values = [
                float(item.metrics[metric])
                for item in report_results
                if item.metrics.get(metric) is not None
            ]
            summary[metric] = sum(values) / len(values) if values else None
        summary["report_quality_case_count"] = len(report_results)
        summary["diagnostic_case_count"] = len(results) - len(report_results)
        return summary


def report_claims(report: ReportAst) -> list[ClaimSegment]:
    output: list[ClaimSegment] = []
    for section in report.sections:
        for block in section.blocks:
            if isinstance(block, ParagraphBlock):
                output.extend(item for item in block.segments if isinstance(item, ClaimSegment))
            elif isinstance(block, ListBlock):
                output.extend(
                    item
                    for list_item in block.items
                    for item in list_item.segments
                    if isinstance(item, ClaimSegment)
                )
    return output


def citation_coverage(
    report: ReportAst,
    state: ResearchState,
    issues: Sequence[Any],
) -> float:
    claims = report_claims(report)
    if not claims:
        return 0.0
    citations = {item.citation_id: item for item in report.citations}
    evidence = {item.evidence_id for item in state.evidence_pool}
    bad_citations = {item.citation_id for item in issues if item.citation_id and item.severity == "error"}
    bad_evidence = {item.evidence_id for item in issues if item.evidence_id and item.severity == "error"}
    covered = 0
    for claim in claims:
        resolvable = False
        for citation_id in claim.citation_ids:
            citation = citations.get(citation_id)
            if (
                citation is not None
                and citation_id not in bad_citations
                and citation.evidence_id in evidence
                and citation.evidence_id not in bad_evidence
            ):
                resolvable = True
                break
        covered += resolvable
    return covered / len(claims)


@dataclass(frozen=True)
class SkillCaseOutput:
    activated_skills: frozenset[str]
    assertion_results: Mapping[str, bool]
    success: bool
    diagnostics: Mapping[str, Any] = field(default_factory=dict)


class SkillEvalSuite(BaseSuite):
    name: EvalSuiteName = "skill"

    def __init__(
        self,
        skill_registry: SkillRegistry,
        skill_runtime: SkillRuntime,
        execute_agent_case: Callable[
            [Mapping[str, Any], bool, int, str, ResourceManifest], SkillCaseOutput
        ],
        *,
        random_seed: int,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self.skill_registry = skill_registry
        self.skill_runtime = skill_runtime
        self.execute_agent_case = execute_agent_case
        self.random_seed = random_seed

    def execute_case(
        self,
        case: Mapping[str, Any],
        eval_scope_id: str,
        manifest: ResourceManifest,
    ) -> EvalCaseResult:
        # Registry/Runtime are intentionally held as production dependencies; the
        # executor drives the same Agent/Tool path with skills toggled at its boundary.
        _ = self.skill_registry, self.skill_runtime
        enabled = self.execute_agent_case(
            case, True, self.random_seed, eval_scope_id, manifest
        )
        disabled = self.execute_agent_case(
            case, False, self.random_seed, eval_scope_id, manifest
        )
        expected = set(case.get("expected_skills") or [])
        actual = set(enabled.activated_skills)
        tp = len(actual & expected)
        fp = len(actual - expected)
        fn = len(expected - actual)
        assertions = list(case.get("result_assertions") or [])
        enabled_effect = all(enabled.assertion_results.get(item, False) for item in assertions)
        disabled_effect = all(disabled.assertion_results.get(item, False) for item in assertions)
        effect_success = bool(expected) and enabled_effect and (
            not disabled_effect or enabled.success >= disabled.success
        )
        return EvalCaseResult(
            case_id=str(case["case_id"]),
            outcome="passed",
            metrics={
                "trigger_tp": tp,
                "trigger_fp": fp,
                "trigger_fn": fn,
                "effect_applicable": bool(expected),
                "effect_success": effect_success,
            },
            diagnostics={
                "actual_activated_skills": sorted(actual),
                "expected_skills": sorted(expected),
                "enabled": dict(enabled.diagnostics),
                "disabled": dict(disabled.diagnostics),
            },
        )

    def summarize(self, results: Sequence[EvalCaseResult]) -> dict[str, Any]:
        tp = sum(int(item.metrics.get("trigger_tp", 0)) for item in results)
        fp = sum(int(item.metrics.get("trigger_fp", 0)) for item in results)
        fn = sum(int(item.metrics.get("trigger_fn", 0)) for item in results)
        precision, recall, f1 = precision_recall_f1(tp, fp, fn)
        effect = [item for item in results if item.metrics.get("effect_applicable")]
        return {
            "trigger_precision": precision,
            "trigger_recall": recall,
            "trigger_f1": f1,
            "effect_success_rate": (
                sum(bool(item.metrics.get("effect_success")) for item in effect) / len(effect)
                if effect
                else None
            ),
        }
