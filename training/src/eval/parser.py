"""Parse Qwen/OpenAI function-call outputs without accepting free-form guesses."""

from __future__ import annotations

import json
import re
from typing import Any

from .schemas import PredictedToolCall


_TOOL_BLOCK = re.compile(r"<tool_call>\s*(\{.*?\})\s*</tool_call>", re.DOTALL)


def _normalize(payload: dict[str, Any]) -> list[dict[str, Any]]:
    if "tool_calls" in payload:
        return payload["tool_calls"]
    if "function" in payload:
        return [payload]
    if "name" in payload:
        return [{"function": payload}]
    raise ValueError("output does not contain a function call")


def parse_tool_calls(value: str | dict[str, Any]) -> list[PredictedToolCall]:
    if isinstance(value, dict):
        payloads = [value]
    else:
        blocks = _TOOL_BLOCK.findall(value)
        if blocks:
            payloads = [json.loads(block) for block in blocks]
        else:
            text = value.strip()
            start, end = text.find("{"), text.rfind("}")
            if start < 0 or end <= start:
                raise ValueError("no JSON object found")
            payloads = [json.loads(text[start:end + 1])]

    calls = []
    for payload in payloads:
        for item in _normalize(payload):
            function = item.get("function", item)
            arguments = function.get("arguments", {})
            if isinstance(arguments, str):
                arguments = json.loads(arguments)
            if not isinstance(arguments, dict):
                raise ValueError("function arguments must be an object")
            calls.append(PredictedToolCall(name=function["name"], arguments=arguments))
    if not calls:
        raise ValueError("empty function-call output")
    return calls
