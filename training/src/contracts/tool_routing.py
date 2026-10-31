"""Pydantic models for the versioned DeepResearch tool-routing API."""

from __future__ import annotations

from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


TOOL_SCHEMA_VERSION = "1.0"


class ToolName(str, Enum):
    WEB_SEARCH = "web_search"
    LOCAL_RAG = "local_rag"
    TEXT2SQL = "text2sql"
    DATA_ANALYSIS = "data_analysis"
    CHART_GENERATION = "chart_generation"


class CapabilitySet(BaseModel):
    model_config = ConfigDict(extra="forbid")

    web_enabled: bool = True
    kb_enabled: bool = False
    kb_name: str | None = None
    database_enabled: bool = False
    database_profile_id: str | None = None

    @model_validator(mode="after")
    def validate_references(self) -> "CapabilitySet":
        if self.kb_enabled and not self.kb_name:
            raise ValueError("kb_name is required when kb_enabled is true")
        if self.database_enabled and not self.database_profile_id:
            raise ValueError(
                "database_profile_id is required when database_enabled is true"
            )
        return self


class ToolCall(BaseModel):
    model_config = ConfigDict(extra="forbid")

    call_id: str = Field(min_length=1, max_length=128)
    name: ToolName
    arguments: dict[str, Any] = Field(default_factory=dict)


class WebSearchArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")
    query: str = Field(min_length=1)
    count: int = Field(default=10, ge=1, le=20)


class LocalRAGArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")
    query: str = Field(min_length=1)
    kb_name: str = Field(min_length=1)
    top_k: int = Field(default=10, ge=1, le=50)


class Text2SQLArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")
    question: str = Field(min_length=1)
    intent: Literal["stats", "trend", "comparison", "detail"]
    database_profile_id: str = Field(min_length=1)


class DataAnalysisArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")
    analysis_type: Literal["auto", "trend", "distribution", "comparison"]
    goal: str = Field(min_length=1)


class ChartGenerationArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")
    chart_type: Literal["line", "bar", "pie", "scatter", "auto"]
    title: str = Field(min_length=1)


TOOL_ARGUMENT_MODELS = {
    ToolName.WEB_SEARCH: WebSearchArguments,
    ToolName.LOCAL_RAG: LocalRAGArguments,
    ToolName.TEXT2SQL: Text2SQLArguments,
    ToolName.DATA_ANALYSIS: DataAnalysisArguments,
    ToolName.CHART_GENERATION: ChartGenerationArguments,
}


class ToolRoutingRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1.0"] = TOOL_SCHEMA_VERSION
    run_id: str = Field(min_length=1, max_length=128)
    session_id: str = Field(min_length=1, max_length=128)
    stage: Literal["research", "analysis"]
    query: str = Field(min_length=1, max_length=10_000)
    context_summary: str | None = Field(default=None, max_length=20_000)
    allowed_tools: list[ToolName] = Field(min_length=1, max_length=5)
    capabilities: CapabilitySet = Field(default_factory=CapabilitySet)

    @model_validator(mode="after")
    def validate_allowed_tools(self) -> "ToolRoutingRequest":
        if len(set(self.allowed_tools)) != len(self.allowed_tools):
            raise ValueError("allowed_tools must not contain duplicates")
        research_tools = {ToolName.WEB_SEARCH, ToolName.LOCAL_RAG, ToolName.TEXT2SQL}
        analysis_tools = {ToolName.DATA_ANALYSIS, ToolName.CHART_GENERATION}
        permitted_stage_tools = research_tools if self.stage == "research" else analysis_tools
        if not set(self.allowed_tools) <= permitted_stage_tools:
            raise ValueError("allowed_tools contains a tool from another stage")
        if ToolName.WEB_SEARCH in self.allowed_tools and not self.capabilities.web_enabled:
            raise ValueError("web_search requires web_enabled")
        if ToolName.LOCAL_RAG in self.allowed_tools and not self.capabilities.kb_enabled:
            raise ValueError("local_rag requires kb_enabled")
        if ToolName.TEXT2SQL in self.allowed_tools and not self.capabilities.database_enabled:
            raise ValueError("text2sql requires database_enabled")
        return self


class ToolRoutingResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision_id: str = Field(min_length=1, max_length=128)
    calls: list[ToolCall] = Field(min_length=1, max_length=3)
    confidence: float = Field(ge=0.0, le=1.0)
    model_version: str = Field(min_length=1, max_length=255)
    adapter_version: str | None = Field(default=None, max_length=255)
    schema_version: Literal["1.0"] = TOOL_SCHEMA_VERSION

    @model_validator(mode="after")
    def calls_must_have_unique_ids(self) -> "ToolRoutingResponse":
        ids = [call.call_id for call in self.calls]
        if len(ids) != len(set(ids)):
            raise ValueError("tool call ids must be unique")
        return self

    def ensure_allowed(self, request: ToolRoutingRequest) -> None:
        disallowed = {call.name for call in self.calls} - set(request.allowed_tools)
        if disallowed:
            names = ", ".join(sorted(item.value for item in disallowed))
            raise ValueError(f"router selected disallowed tools: {names}")
        for call in self.calls:
            TOOL_ARGUMENT_MODELS[call.name].model_validate(call.arguments)
