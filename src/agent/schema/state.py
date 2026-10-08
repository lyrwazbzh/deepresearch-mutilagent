from __future__ import annotations

from datetime import datetime, timezone
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)


class AgentName(StrEnum):
    DEEP_SCOUT = "deep_scout"
    DATA_ANALYST = "data_analyst"
    CODE_WIZARD = "code_wizard"


class RouteDecision(StrictModel):
    mode: Literal["direct", "focused", "deep"]
    target_agent: AgentName | None = None

    @model_validator(mode="after")
    def validate_target(self) -> "RouteDecision":
        if self.mode == "focused" and self.target_agent is None:
            raise ValueError("target_agent is required for focused route")
        if self.mode != "focused" and self.target_agent is not None:
            raise ValueError("target_agent is only valid for focused route")
        return self


class ResearchQuery(StrictModel):
    query_id: str = Field(min_length=1)
    channel: Literal["web", "rag", "sql"]
    text: str = Field(min_length=1)


class Hypothesis(StrictModel):
    hypothesis_id: str = Field(min_length=1)
    claim: str = Field(min_length=1)
    evidence_needed: list[str] = Field(min_length=1)
    refuted_if: list[str] = Field(min_length=1)
    priority: int = Field(ge=0)
    queries: list[ResearchQuery] = Field(min_length=1)


class WriteIntent(StrictModel):
    intent_id: str
    description: str
    datasource_id: str | None = None


class DeepenRecord(StrictModel):
    depth: int = Field(ge=1)
    reason: str
    added_hypothesis_ids: list[str] = Field(default_factory=list)
    added_query_ids: list[str] = Field(default_factory=list)


class HypothesisPlan(StrictModel):
    hypotheses: list[Hypothesis] = Field(min_length=1)
    write_intent: WriteIntent | None = None
    deepen_history: list[DeepenRecord] = Field(default_factory=list)

    @model_validator(mode="after")
    def identifiers_are_unique(self) -> "HypothesisPlan":
        hypothesis_ids = [item.hypothesis_id for item in self.hypotheses]
        query_ids = [query.query_id for item in self.hypotheses for query in item.queries]
        if len(hypothesis_ids) != len(set(hypothesis_ids)):
            raise ValueError("hypothesis_id values must be unique")
        if len(query_ids) != len(set(query_ids)):
            raise ValueError("query_id values must be unique")
        return self


class Evidence(StrictModel):
    evidence_id: str
    hypothesis_id: str
    summary: str
    excerpt: str
    source_type: Literal["web", "rag", "sql", "compute"]
    stance: Literal["support", "refute", "context"]
    relevance: float = Field(ge=0, le=1)
    data: dict[str, Any] | None = None
    doc_id: str | None = None
    chunk_id: str | None = None
    workspace_path: str | None = None
    source_uri: str | None = None
    published_at: datetime | None = None
    retrieved_at: datetime | None = None
    content_hash: str
    derived_from: list[str] | None = None
    execution_ref: str | None = None

    @model_validator(mode="after")
    def validate_location(self) -> "Evidence":
        locations = int(self.data is not None) + int(self.workspace_path is not None) + int(
            self.doc_id is not None or self.chunk_id is not None
        )
        if locations != 1:
            raise ValueError("Evidence must use exactly one content location")
        if (self.doc_id is None) != (self.chunk_id is None):
            raise ValueError("doc_id and chunk_id must be provided together")
        if self.source_type == "compute" and not (self.derived_from and self.execution_ref):
            raise ValueError("compute evidence requires derived_from and execution_ref")
        if self.source_type == "web" and not (self.source_uri and self.retrieved_at):
            raise ValueError("web evidence requires source_uri and retrieved_at")
        if self.source_type == "rag" and not (self.doc_id and self.chunk_id):
            raise ValueError("rag evidence requires doc_id and chunk_id")
        if self.source_type in {"sql", "compute"} and self.source_uri is not None:
            raise ValueError(f"{self.source_type} evidence does not accept source_uri")
        return self


class AnalysisRequest(StrictModel):
    analysis_id: str = Field(min_length=1)
    hypothesis_id: str = Field(min_length=1)
    objective: str = Field(min_length=1)
    input_evidence_ids: list[str] = Field(min_length=1)
    expected_output: str = Field(min_length=1)
    status: Literal["pending", "completed", "failed"] = "pending"

    @model_validator(mode="after")
    def evidence_ids_are_unique(self) -> "AnalysisRequest":
        if len(self.input_evidence_ids) != len(set(self.input_evidence_ids)):
            raise ValueError("input_evidence_ids must be unique")
        return self


class CoverageGap(StrictModel):
    hypothesis_id: str
    gap_type: Literal[
        "missing_evidence", "missing_channel", "pending_query", "pending_analysis"
    ]
    channel: Literal["web", "rag", "sql"] | None = None
    description: str


class CoverageResult(StrictModel):
    complete: bool
    gaps: list[CoverageGap] = Field(default_factory=list)
    stop_reason: Literal["max_depth", "budget_exhausted", "no_new_queries"] | None = None

    @model_validator(mode="after")
    def validate_shape(self) -> "CoverageResult":
        if self.complete and (self.gaps or self.stop_reason is not None):
            raise ValueError("complete coverage cannot contain gaps or a stop_reason")
        if not self.complete and not self.gaps:
            raise ValueError("incomplete coverage requires at least one gap")
        return self


class CriticGap(StrictModel):
    hypothesis_id: str
    reason: str
    target_channel: Literal["web", "rag", "sql"] | None = None


class CriticResult(StrictModel):
    score: int = Field(ge=0, le=10)
    decision: Literal["pass", "deepen", "retrieve"]
    gaps: list[CriticGap] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_decision(self) -> "CriticResult":
        if self.decision == "pass" and self.score < 8:
            raise ValueError("pass requires score >= 8")
        if self.decision == "pass" and self.gaps:
            raise ValueError("pass cannot contain critic gaps")
        if self.decision != "pass" and not self.gaps:
            raise ValueError("deepen/retrieve requires at least one critic gap")
        if self.decision == "retrieve" and any(
            gap.target_channel is None for gap in self.gaps
        ):
            raise ValueError("retrieve gaps require target_channel")
        if self.decision == "deepen" and any(
            gap.target_channel is not None for gap in self.gaps
        ):
            raise ValueError("deepen gaps cannot target a retrieval channel")
        return self


class ReportArtifact(StrictModel):
    version: int = Field(ge=1)
    workspace_path: str
    content_hash: str


class ReportState(StrictModel):
    draft: ReportArtifact | None = None
    final: ReportArtifact | None = None


class ExecutionState(StrictModel):
    depth: int = Field(default=0, ge=0)
    max_depth: int = Field(default=3, ge=0, le=5)
    revision_round: int = Field(default=0, ge=0)
    max_revision_rounds: int = Field(default=2, ge=0, le=5)


class MemoryItemRef(StrictModel):
    memory_id: str
    revision: int = Field(ge=1)
    content_hash: str


class MemoryContext(StrictModel):
    query_hash: str
    items: list[MemoryItemRef] = Field(default_factory=list)


class AdoptedResult(StrictModel):
    hypothesis_id: str = Field(min_length=1)
    result_key: str = Field(min_length=32)
    summary: str = Field(min_length=1)
    excerpt: str = Field(min_length=1)
    stance: Literal["support", "refute", "context"]
    relevance: float = Field(ge=0, le=1)
    content_hash: str = Field(min_length=32)
    data: dict[str, Any] | None = None
    doc_id: str | None = None
    chunk_id: str | None = None
    workspace_path: str | None = None
    source_uri: str | None = None
    published_at: datetime | None = None
    retrieved_at: datetime | None = None
    derived_from: list[str] | None = None
    execution_ref: str | None = None

    @model_validator(mode="after")
    def validate_location(self) -> "AdoptedResult":
        document = self.doc_id is not None or self.chunk_id is not None
        locations = int(self.data is not None) + int(self.workspace_path is not None) + int(document)
        if locations != 1:
            raise ValueError("adopted result must use exactly one content location")
        if (self.doc_id is None) != (self.chunk_id is None):
            raise ValueError("doc_id and chunk_id must be provided together")
        return self


class AnalysisResult(StrictModel):
    analysis_id: str = Field(min_length=1)
    status: Literal["completed", "failed"]
    adopted_results: list[AdoptedResult] = Field(default_factory=list)
    error: dict[str, Any] | None = None

    @model_validator(mode="after")
    def validate_result(self) -> "AnalysisResult":
        if self.status == "completed" and not self.adopted_results:
            raise ValueError("completed analysis requires adopted compute results")
        if self.status == "completed" and self.error is not None:
            raise ValueError("completed analysis cannot contain an error")
        if self.status == "failed" and self.error is None:
            raise ValueError("failed analysis requires an error")
        return self


class BranchResult(StrictModel):
    status: Literal["pending", "completed", "failed", "skipped"]
    items: list[AdoptedResult] = Field(default_factory=list)
    error: dict[str, Any] | None = None

    @model_validator(mode="after")
    def validate_terminal_shape(self) -> "BranchResult":
        if self.status in {"pending", "skipped"} and self.items:
            raise ValueError(f"{self.status} branch cannot contain adopted items")
        if self.status == "pending" and self.error is not None:
            raise ValueError("pending branch cannot contain an error")
        return self


class ResearchRound(StrictModel):
    web: BranchResult
    rag: BranchResult
    sql: BranchResult
    compute: BranchResult | None = None


class ReliabilityState(StrictModel):
    errors: list[dict[str, Any]] = Field(default_factory=list)
    fallbacks: list[dict[str, Any]] = Field(default_factory=list)
    degraded_branches: list[dict[str, Any]] = Field(default_factory=list)


class StateTimestamps(StrictModel):
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class ResearchState(StrictModel):
    schema_version: str = "1.0"
    tenant_id: str
    user_id: str
    thread_id: str
    task_id: str
    query: str = Field(min_length=1)
    execution: ExecutionState = Field(default_factory=ExecutionState)
    route: RouteDecision | None = None
    plan: HypothesisPlan | None = None
    memory_context: MemoryContext | None = None
    research_rounds: dict[str, ResearchRound] = Field(default_factory=dict)
    evidence_pool: list[Evidence] = Field(default_factory=list)
    analysis_requests: list[AnalysisRequest] = Field(default_factory=list)
    coverage: CoverageResult | None = None
    critic: CriticResult | None = None
    pending_write: str | None = None
    report: ReportState = Field(default_factory=ReportState)
    reliability: ReliabilityState = Field(default_factory=ReliabilityState)
    timestamps: StateTimestamps = Field(default_factory=StateTimestamps)

    @model_validator(mode="after")
    def plan_matches_route(self) -> "ResearchState":
        if self.plan is not None and (self.route is None or self.route.mode != "deep"):
            raise ValueError("plan may only exist for the deep route")
        return self
