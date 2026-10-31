"""Judge metric facade mirroring the reference project's evaluation layer."""

from __future__ import annotations

from .llm_judge import LLMJudge
from .schemas import EvalCase, JudgeScores


class JudgeBasedMetrics:
    def __init__(self, judge: LLMJudge) -> None:
        self.judge = judge

    async def judge_score(self, case: EvalCase) -> JudgeScores:
        return await self.judge.evaluate(case)
