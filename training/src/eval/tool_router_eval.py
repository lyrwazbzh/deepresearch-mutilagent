"""Held-out tool-selection and argument evaluation."""

from __future__ import annotations

import inspect
import json
from collections import defaultdict
from pathlib import Path

from jsonschema import Draft202012Validator

from src.data.schema_v3 import Trajectory, load_tool_schema

from .parser import parse_tool_calls
from .schemas import RoutingMetrics, StepResult


def evaluation_steps(records: list[Trajectory]):
    for record in records:
        prefix = []
        step_index = 0
        for message in record.messages:
            dumped = message.model_dump(mode="json", exclude_none=True)
            if message.role == "assistant" and message.tool_calls:
                yield record, step_index, list(prefix), message.tool_calls
                step_index += 1
            prefix.append(dumped)


async def evaluate(records, generate, *, tool_schema_path: str) -> tuple[RoutingMetrics, list[StepResult]]:
    schema = load_tool_schema(tool_schema_path)
    results = []
    trajectory_steps = defaultdict(list)
    for trajectory, step_index, prefix, expected_calls in evaluation_steps(records):
        output = generate(prefix)
        if inspect.isawaitable(output):
            output = await output
        parse_error = None
        predicted = []
        json_valid = True
        schema_valid = True
        try:
            predicted = parse_tool_calls(output)
            for call in predicted:
                spec = schema.get(call.name)
                if not spec:
                    schema_valid = False
                    continue
                schema_valid = schema_valid and not list(
                    Draft202012Validator(spec["parameters"]).iter_errors(call.arguments)
                )
        except (ValueError, KeyError, json.JSONDecodeError) as exc:
            parse_error = str(exc)
            json_valid = False
            schema_valid = False
        expected_names = [call.function.name.value for call in expected_calls]
        predicted_names = [call.name for call in predicted]
        item = StepResult(
            trajectory_id=trajectory.metadata.trajectory_id,
            step_index=step_index,
            expected_tools=expected_names,
            predicted_tools=predicted_names,
            tool_exact=predicted_names == expected_names,
            arguments_json_valid=json_valid,
            arguments_schema_valid=schema_valid,
            parse_error=parse_error,
        )
        results.append(item)
        trajectory_steps[item.trajectory_id].append(item.tool_exact and schema_valid)

    total = len(results)
    def ratio(count: int) -> float:
        return count / total if total else 0.0
    successful_trajectories = sum(all(items) for items in trajectory_steps.values())
    metrics = RoutingMetrics(
        total_steps=total,
        tool_selection_accuracy=ratio(sum(item.tool_exact for item in results)),
        argument_json_validity=ratio(sum(item.arguments_json_valid for item in results)),
        argument_schema_validity=ratio(sum(item.arguments_schema_valid for item in results)),
        trajectory_success_rate=(
            successful_trajectories / len(trajectory_steps) if trajectory_steps else 0.0
        ),
    )
    return metrics, results


def load_records(path: str | Path) -> list[Trajectory]:
    return [
        Trajectory.model_validate_json(line)
        for line in Path(path).read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def release_gate(
    candidate: RoutingMetrics,
    *,
    baseline: RoutingMetrics | None = None,
    min_tool_accuracy: float = 0.90,
    min_schema_validity: float = 0.98,
    max_regression: float = 0.01,
) -> dict:
    reasons = []
    if candidate.tool_selection_accuracy < min_tool_accuracy:
        reasons.append("tool_selection_accuracy_below_threshold")
    if candidate.argument_schema_validity < min_schema_validity:
        reasons.append("argument_schema_validity_below_threshold")
    if baseline and (
        candidate.tool_selection_accuracy
        < baseline.tool_selection_accuracy - max_regression
    ):
        reasons.append("tool_selection_accuracy_regressed")
    return {"passed": not reasons, "reasons": reasons}
