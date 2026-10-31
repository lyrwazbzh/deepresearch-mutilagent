"""Cross-domain deep-research benchmark adapted from deepresearch-agent-main."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..rule_metrics import RuleBasedMetrics, evaluate_rules
from ..schemas import EvalCase


class ResearchBench:
    """Load ResearchBench cases and evaluate complete research reports."""

    DEFAULT_QUESTIONS: list[dict[str, Any]] = [
        {
            "id": "tech_001",
            "domain": "科技",
            "query": "对比主流大语言模型在中文推理、代码生成和长上下文任务上的差异。",
            "expected_topics": ["中文推理", "代码生成", "长上下文", "技术路线"],
            "ground_truth": {
                "GPT-4o": "OpenAI 的原生多模态模型",
                "Qwen2.5": "开源并支持长上下文",
            },
        },
        {
            "id": "fin_001",
            "domain": "金融",
            "query": "分析利率周期对全球资本流动、新兴市场汇率和中国货币政策的影响。",
            "expected_topics": ["利率", "资本流动", "新兴市场", "汇率", "货币政策"],
            "ground_truth": {
                "资本流动": "利差变化会影响跨境资本配置",
                "汇率": "利率预期会通过资金流影响汇率",
            },
        },
        {
            "id": "energy_001",
            "domain": "能源",
            "query": "比较锂离子、钠离子和固态电池的技术路线与商业化约束。",
            "expected_topics": ["锂离子", "钠离子", "固态电池", "成本", "安全性"],
            "ground_truth": {
                "钠离子": "资源成本较低但能量密度受限",
                "固态电池": "安全性潜力较高但制造仍具挑战",
            },
        },
    ]

    def __init__(
        self,
        data_path: str | Path | None = None,
        *,
        questions: list[dict[str, Any]] | None = None,
    ) -> None:
        if questions is not None:
            self.questions = questions
        elif data_path is not None:
            self.questions = self._load(Path(data_path))
        else:
            self.questions = list(self.DEFAULT_QUESTIONS)

    @staticmethod
    def _load(path: Path) -> list[dict[str, Any]]:
        if path.suffix == ".jsonl":
            return [
                json.loads(line)
                for line in path.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, list):
            raise ValueError("ResearchBench JSON must contain a list of cases")
        return payload

    def get_questions(
        self,
        domain: str | None = None,
        n: int | None = None,
    ) -> list[dict[str, Any]]:
        result = self.questions
        if domain is not None:
            result = [question for question in result if question.get("domain") == domain]
        return result[:n] if n is not None else list(result)

    def evaluate_report(
        self,
        report: str,
        question_id: str,
        metrics_weights: dict[str, float] | None = None,
    ) -> dict[str, Any]:
        question = next(
            (item for item in self.questions if item["id"] == question_id),
            None,
        )
        if question is None:
            raise ValueError(f"unknown ResearchBench question: {question_id}")
        case = EvalCase(
            id=question_id,
            query=question["query"],
            report=report,
            required_topics=question.get("expected_topics", []),
            ground_truth=question.get("ground_truth", {}),
            metadata={"domain": question.get("domain", "")},
        )
        metrics = evaluate_rules(case)
        return {
            "question_id": question_id,
            "domain": question.get("domain", ""),
            "metrics": metrics.model_dump(mode="json"),
            "composite_score": RuleBasedMetrics.composite_score(
                metrics.quality_dimensions(),
                metrics_weights,
            ),
        }

    def batch_evaluate(self, results: list[dict[str, Any]]) -> dict[str, Any]:
        details = [
            self.evaluate_report(item["report"], item["question_id"])
            for item in results
        ]
        if not details:
            return {"average_composite": 0.0, "by_domain": {}, "details": []}
        by_domain: dict[str, list[float]] = {}
        for detail in details:
            by_domain.setdefault(detail["domain"], []).append(detail["composite_score"])
        return {
            "average_composite": sum(
                detail["composite_score"] for detail in details
            ) / len(details),
            "by_domain": {
                domain: sum(scores) / len(scores)
                for domain, scores in by_domain.items()
            },
            "details": details,
        }
