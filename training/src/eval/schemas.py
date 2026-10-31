"""Evaluation result contracts for the tool-routing adapter."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class PredictedToolCall(BaseModel):
    name: str
    arguments: dict[str, Any]


class StepResult(BaseModel):
    trajectory_id: str
    step_index: int
    expected_tools: list[str]
    predicted_tools: list[str]
    tool_exact: bool
    arguments_json_valid: bool
    arguments_schema_valid: bool
    parse_error: str | None = None


class RoutingMetrics(BaseModel):
    total_steps: int = Field(ge=0)
    tool_selection_accuracy: float = Field(ge=0.0, le=1.0)
    argument_json_validity: float = Field(ge=0.0, le=1.0)
    argument_schema_validity: float = Field(ge=0.0, le=1.0)
    trajectory_success_rate: float = Field(ge=0.0, le=1.0)


class EvaluationReport(BaseModel):
    model: str
    adapter: str | None = None
    dataset: str
    tool_schema_sha256: str
    metrics: RoutingMetrics
    steps: list[StepResult]
