"""Canonical schema and validation for tool-routing SFT trajectories."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from jsonschema import Draft202012Validator
from pydantic import BaseModel, ConfigDict, Field, model_validator

from src.contracts.tool_routing import ToolName


SCHEMA_VERSION = "3.0"


class FunctionCall(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: ToolName
    arguments: str

    def parsed_arguments(self) -> dict[str, Any]:
        value = json.loads(self.arguments)
        if not isinstance(value, dict):
            raise ValueError("function arguments must decode to an object")
        return value


class AssistantToolCall(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=128)
    type: Literal["function"] = "function"
    function: FunctionCall


class TrajectoryMessage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    role: Literal["system", "user", "assistant", "tool"]
    content: str | None = None
    tool_calls: list[AssistantToolCall] | None = None
    tool_call_id: str | None = None

    @model_validator(mode="after")
    def validate_role_shape(self) -> "TrajectoryMessage":
        if self.role == "assistant" and not self.content and not self.tool_calls:
            raise ValueError("assistant messages require content or tool_calls")
        if self.role == "tool" and not self.tool_call_id:
            raise ValueError("tool messages require tool_call_id")
        if self.role != "assistant" and self.tool_calls:
            raise ValueError("only assistant messages may contain tool_calls")
        if self.role != "tool" and self.tool_call_id:
            raise ValueError("only tool messages may contain tool_call_id")
        return self


class TrajectoryMetadata(BaseModel):
    model_config = ConfigDict(extra="allow")

    trajectory_id: str = Field(min_length=1, max_length=128)
    seed_id: str = Field(min_length=1, max_length=128)
    industry: str = Field(min_length=1, max_length=128)
    question_type: str = Field(min_length=1, max_length=128)
    difficulty: Literal["easy", "medium", "hard"] = "medium"
    teacher_model: str = Field(min_length=1, max_length=255)
    tools_used: list[ToolName] = Field(default_factory=list)
    quality_score: float = Field(default=0.0, ge=0.0, le=1.0)
    schema_version: Literal["3.0"] = SCHEMA_VERSION
    tool_schema_version: Literal["1.0"] = "1.0"
    content_hash: str = ""
    generation_batch: str = "default"
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc)
    )


class Trajectory(BaseModel):
    model_config = ConfigDict(extra="forbid")

    messages: list[TrajectoryMessage] = Field(min_length=3)
    metadata: TrajectoryMetadata

    @model_validator(mode="after")
    def validate_sequence(self) -> "Trajectory":
        if self.messages[0].role != "system":
            raise ValueError("first message must be system")
        if self.messages[1].role != "user":
            raise ValueError("second message must be user")

        call_ids: set[str] = set()
        answered_ids: set[str] = set()
        tools: list[ToolName] = []
        for message in self.messages:
            for call in message.tool_calls or []:
                if call.id in call_ids:
                    raise ValueError(f"duplicate tool call id: {call.id}")
                call.function.parsed_arguments()
                call_ids.add(call.id)
                tools.append(call.function.name)
            if message.role == "tool":
                if message.tool_call_id not in call_ids:
                    raise ValueError(
                        f"tool response references unknown call: {message.tool_call_id}"
                    )
                if message.tool_call_id in answered_ids:
                    raise ValueError(
                        f"duplicate tool response: {message.tool_call_id}"
                    )
                answered_ids.add(message.tool_call_id)

        if not call_ids:
            raise ValueError("trajectory must contain at least one tool call")
        self.metadata.tools_used = list(dict.fromkeys(tools))
        return self

    def canonical_payload(self) -> bytes:
        payload = self.model_dump(mode="json")
        metadata = payload["metadata"]
        metadata["content_hash"] = ""
        metadata["created_at"] = ""
        return json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")

    def refresh_content_hash(self) -> str:
        digest = hashlib.sha256(self.canonical_payload()).hexdigest()
        self.metadata.content_hash = digest
        return digest


def load_tool_schema(path: str | Path) -> dict[str, dict[str, Any]]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if payload.get("schema_version") != "1.0":
        raise ValueError("unsupported tool schema version")
    tools = payload.get("tools")
    if not isinstance(tools, list):
        raise ValueError("tool schema must contain a tools array")
    return {tool["name"]: tool for tool in tools}


def validate_tool_arguments(
    trajectory: Trajectory,
    tool_schema: dict[str, dict[str, Any]],
) -> list[str]:
    errors: list[str] = []
    for message in trajectory.messages:
        for call in message.tool_calls or []:
            name = call.function.name.value
            spec = tool_schema.get(name)
            if not spec:
                errors.append(f"unknown tool: {name}")
                continue
            arguments = call.function.parsed_arguments()
            validator = Draft202012Validator(spec["parameters"])
            for error in validator.iter_errors(arguments):
                errors.append(f"{name}: {error.message}")
    return errors
