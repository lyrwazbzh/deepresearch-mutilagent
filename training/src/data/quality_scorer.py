"""Deterministic quality score for generated tool-calling trajectories."""

from __future__ import annotations

from src.data.schema_v3 import Trajectory


def score_trajectory(trajectory: Trajectory) -> float:
    calls = [
        call
        for message in trajectory.messages
        for call in (message.tool_calls or [])
    ]
    tool_response_messages = {
        message.tool_call_id: message.content
        for message in trajectory.messages
        if message.role == "tool"
    }
    score = 0.30
    if calls:
        score += 0.20
    if calls and all(call.id in tool_response_messages for call in calls):
        score += 0.20
    if calls and all(call.function.parsed_arguments() for call in calls):
        score += 0.10
    if calls and all(str(tool_response_messages.get(call.id, "")).strip() for call in calls):
        score += 0.05
    if len({call.function.name for call in calls}) > 1:
        score += 0.05
    if trajectory.messages[-1].role == "assistant" and trajectory.messages[-1].content:
        score += 0.10
    return min(round(score, 4), 1.0)
