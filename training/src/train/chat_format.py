"""Canonical OpenAI-style trajectory to tokenizer chat-template conversion."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def load_openai_tools(path: str | Path) -> list[dict[str, Any]]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    return [
        {
            "type": "function",
            "function": {
                "name": tool["name"],
                "description": tool["description"],
                "parameters": tool["parameters"],
            },
        }
        for tool in payload["tools"]
    ]


def format_trajectory_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    formatted = []
    for message in messages:
        role = message["role"]
        if role not in {"system", "user", "assistant", "tool"}:
            raise ValueError(f"unsupported role: {role}")
        item: dict[str, Any] = {"role": role, "content": message.get("content")}
        if role == "assistant" and message.get("tool_calls"):
            item["tool_calls"] = message["tool_calls"]
        if role == "tool":
            item["tool_call_id"] = message["tool_call_id"]
        formatted.append(item)
    return formatted
