"""Structured LLM-as-Judge adapter aligned with deepresearch-agent-main."""

from __future__ import annotations

import inspect
import json
import re
from collections.abc import Callable

from .schemas import EvalCase, JudgeScores


JUDGE_PROMPT = """你是独立的深度研究报告评测员。请依据问题、报告、参考来源和关键事实评分。

每项 0-10 分：
1. factual_accuracy：数字、日期、实体和关键事实是否准确；
2. logical_consistency：论证是否自洽，结论能否由证据推出；
3. citation_quality：来源是否可靠、充分，并能支持对应主张；
4. comprehensiveness：是否覆盖问题的主要子问题；
5. overall：整体研究质量。

问题：{query}
关键事实：{ground_truth}
参考来源：{references}
报告：
{report}

只输出以下 JSON，不要输出 Markdown：
{{
  "factual_accuracy": {{"score": 0-10, "reason": "简短理由"}},
  "logical_consistency": {{"score": 0-10, "reason": "简短理由"}},
  "citation_quality": {{"score": 0-10, "reason": "简短理由"}},
  "comprehensiveness": {{"score": 0-10, "reason": "简短理由"}},
  "overall": {{"score": 0-10, "reason": "简短理由"}}
}}"""


class LLMJudge:
    """Use an injected sync/async generator to avoid coupling evals to a provider."""

    def __init__(self, generate: Callable[[str], str], *, backend: str = "callable"):
        self.generate = generate
        self.backend = backend

    async def evaluate(self, case: EvalCase) -> JudgeScores:
        prompt = JUDGE_PROMPT.format(
            query=case.query,
            ground_truth=json.dumps(case.ground_truth, ensure_ascii=False)[:6000],
            references=json.dumps(
                [item.model_dump(mode="json") for item in case.references],
                ensure_ascii=False,
            )[:8000],
            report=case.report[:24000],
        )
        response = self.generate(prompt)
        if inspect.isawaitable(response):
            response = await response
        payload = self._parse_json(str(response))

        # Also accept the reference judge's public {dimensions, overall} shape.
        if "dimensions" in payload:
            dimensions = payload["dimensions"]
            payload = {
                **dimensions,
                "overall": payload.get("overall", {"score": 0, "reason": ""}),
            }
        payload["judge_backend"] = self.backend
        return JudgeScores.model_validate(payload)

    @staticmethod
    def _parse_json(text: str) -> dict:
        text = text.strip()
        fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
        if fenced:
            text = fenced.group(1)
        else:
            start, end = text.find("{"), text.rfind("}")
            if start >= 0 and end > start:
                text = text[start : end + 1]
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            cleaned = text.replace("'", '"')
            cleaned = re.sub(r",(\s*[}\]])", r"\1", cleaned)
            return json.loads(cleaned)
