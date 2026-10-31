# Copyright © 2026 深圳市深维智见教育科技有限公司 版权所有
# 未经授权，禁止转售或仿制。

"""
DeepResearch V2.0 - 毒舌评论家 Agent (CriticMaster)

职责：
1. 对抗式质检 - 永远不满意，找出问题
2. 逻辑漏洞检测 - 检查推理链条
3. 幻觉查杀 - 识别无来源或错误的信息
4. 偏见识别 - 发现观点偏颇
"""

import uuid
from typing import Dict, Any, List
from datetime import datetime

from .base import BaseAgent
from ..state import ResearchState, ResearchPhase
from observability.events import record_research_event


class CriticMaster(BaseAgent):
    """
    毒舌评论家 - 质量守门人

    特点：
    - 对抗式思维：假设一切都有问题
    - 严格的证据要求
    - 逻辑一致性检查
    - 有权打回重写
    """

    # Prompt 只负责让模型按口径评分；是否放行必须由确定性代码决定。
    MIN_PASS_SCORE = 7.0
    BLOCKING_SEVERITIES = {"critical", "major"}
    ALLOWED_SEVERITIES = {"critical", "major", "minor"}
    ALLOWED_ISSUE_TYPES = {
        "missing_source", "logic_error", "bias", "hallucination",
        "outdated", "incomplete",
    }
    ALLOWED_FACT_STATUSES = {"verified", "unverified", "suspicious", "false"}
    BLOCKING_FACT_STATUSES = {"unverified", "suspicious", "false"}

    REVIEW_PROMPT = """你是一位极其严苛的学术审稿人和事实核查专家。你的任务是找出研究报告中的所有问题。

## 审核原则（必须严格执行）
1. **零容忍幻觉**：任何没有明确来源的数据或事实，都是问题
2. **逻辑闭环**：论点必须有论据支撑，论据必须有来源
3. **偏见警惕**：单方面观点、情绪化表达都是问题
4. **时效性**：过时的数据（超过2年）必须标注
5. **完整性**：是否遗漏重要方面

## 研究问题
{query}

## 研究大纲
{outline}

## 待审核内容

### 章节草稿
{draft_content}

### 引用的事实
{facts}

### 使用的数据点
{data_points}

## 任务
逐条审核上述内容，找出所有问题。你必须扮演一个"找茬专家"的角色。

## 输出格式
```json
{{
    "overall_assessment": {{
        "quality_score": 1-10,
        "verdict": "pass/needs_revision/major_issues",
        "summary": "整体评估摘要"
    }},
    "issues": [
        {{
            "id": "issue_1",
            "target_section": "章节ID或'全局'",
            "issue_type": "missing_source/logic_error/bias/hallucination/outdated/incomplete",
            "severity": "critical/major/minor",
            "location": "具体位置描述",
            "description": "问题详细描述",
            "evidence": "为什么这是问题的证据",
            "suggestion": "具体的修改建议",
            "requires_new_search": true或false,
            "search_query": "如果需要补充搜索，建议的关键词"
        }}
    ],
    "fact_check_results": [
        {{
            "fact_id": "事实ID",
            "status": "verified/unverified/suspicious/false",
            "reason": "判断理由"
        }}
    ],
    "missing_aspects": ["报告中遗漏的重要方面"],
    "strength_points": ["报告中做得好的地方"]
}}
```

## 严重程度说明
- critical: 必须修复，否则报告不可用（如：核心数据错误、严重幻觉）
- major: 强烈建议修复，影响报告质量（如：缺少来源、逻辑漏洞）
- minor: 建议修复，提升报告质量（如：表述不够精确）

## 评分标准（1-10分制）
- 9-10分：优秀，几乎无问题，可直接发布
- 7-8分：良好，有小问题但不影响整体质量，审核通过（verdict=pass）
- 5-6分：一般，有明显问题需要修订
- 3-4分：较差，问题较多，需要大幅修改
- 1-2分：很差，存在严重问题或大量错误

注意：quality_score >= 7 时才能设置 verdict 为 "pass"

开始你的审核："""

    FINAL_CHECK_PROMPT = """你是最终质量把关人。这是修订后的研究报告。

## 原始问题
{query}

## 之前的问题
{previous_issues}

## 修订后的内容
{revised_content}

## 任务
检查之前的问题是否已解决，是否有新问题产生。

输出JSON：
```json
{{
    "resolved_issues": ["已解决的问题ID列表"],
    "unresolved_issues": ["未解决的问题ID列表"],
    "new_issues": [{{
        "description": "新发现的问题",
        "severity": "critical/major/minor"
    }}],
    "final_verdict": "approved/needs_more_work",
    "final_score": 1-10,
    "publication_readiness": "ready/almost_ready/not_ready",
    "final_comments": "最终评语"
}}
```"""

    def __init__(self, llm_api_key: str, llm_base_url: str, model: str = "qwen-max"):
        super().__init__(
            name="CriticMaster",
            role="毒舌评论家",
            llm_api_key=llm_api_key,
            llm_base_url=llm_base_url,
            model=model
        )

    async def process(self, state: ResearchState) -> ResearchState:
        """处理入口"""
        self.logger.info(f"[CriticMaster] ========== process 开始 ==========")
        self.logger.info(f"[CriticMaster] phase: {state['phase']}, final_report 长度: {len(state.get('final_report', ''))}")

        if state["phase"] != ResearchPhase.REVIEWING.value:
            self.logger.info(f"[CriticMaster] phase 不是 REVIEWING，跳过")
            return state

        self.add_message(state, "thought", {
            "agent": self.name,
            "content": "开始严格审核研究报告，准备找出所有问题..."
        })

        # 执行审核
        self.logger.info(f"[CriticMaster] 开始调用 _review_content...")
        review_result = await self._review_content(state)
        self.logger.info(f"[CriticMaster] 审核完成，结果: {bool(review_result)}")

        gate = self._evaluate_quality_gate(review_result)
        normalized_result = gate["review_result"]
        issues = normalized_result["issues"]
        missing_aspects = normalized_result["missing_aspects"]
        fact_check_failures = gate["fact_check_failures"]
        review_round = int(state.get("iteration", 0)) + 1

        # 无法解析或字段不合法也必须 fail-closed，生成一条可追踪的阻断问题。
        if gate["invalid_response"]:
            issues.append({
                "issue_type": "critic_output_invalid",
                "severity": "major",
                "target_section": "全局",
                "description": "Critic 返回结果缺失或不符合质量门禁契约",
                "suggestion": "重新执行质量审核，禁止在审核结果无效时发布报告",
                "requires_new_search": False,
            })

        for issue in issues:
            issue["id"] = f"issue_{uuid.uuid4().hex[:8]}"
            issue["resolved"] = False
            state["critic_feedback"].append(issue)

        state["quality_score"] = gate["quality_score"]
        state["quality_gate_passed"] = gate["passed"]
        state["quality_gate_reasons"] = gate["reasons"]
        state["unresolved_issues"] = (
            sum(
            issue.get("severity") in self.BLOCKING_SEVERITIES for issue in issues
            )
            + len(missing_aspects)
            + len(fact_check_failures)
        )

        next_action = "complete"
        search_queries: List[str] = []
        if gate["passed"]:
            state["phase"] = ResearchPhase.COMPLETED.value
        elif review_round >= max(int(state.get("max_iterations", 1)), 1):
            # 达到上限仍不合格：失败而不是伪装成正常完成。
            next_action = "quality_gate_failed"
            state["phase"] = ResearchPhase.QUALITY_GATE_FAILED.value
            self.add_message(state, "warning", {
                "agent": self.name,
                "content": "已达到质量审核上限，报告仍未通过发布门禁",
                "quality_gate_reasons": gate["reasons"],
            })
        else:
            routing = self._analyze_issues_for_routing(normalized_result)
            if routing["should_research"]:
                next_action = "re_research"
                search_queries = routing["search_queries"]
                state["phase"] = ResearchPhase.RE_RESEARCHING.value
                state["pending_search_queries"] = search_queries
                self.add_message(state, "thought", {
                    "agent": self.name,
                    "content": f"发现信息缺失问题，需要补充搜索: {', '.join(search_queries[:3])}"
                })
            else:
                next_action = "revise"
                state["phase"] = ResearchPhase.REVISING.value

        state["iteration"] = review_round
        review_entry = {
            "round": review_round,
            "model_verdict": gate["model_verdict"],
            "quality_score": gate["quality_score"],
            "gate_passed": gate["passed"],
            "gate_reasons": gate["reasons"],
            "issue_types": sorted({
                *[str(item.get("issue_type", "unknown")) for item in issues],
                *[
                    f"fact_check_{item.get('status', 'invalid')}"
                    for item in fact_check_failures
                ],
            }),
            "blocking_issues": state["unresolved_issues"],
            "missing_aspects": missing_aspects,
            "fact_check_failures": fact_check_failures,
            "suggestions": [
                str(item.get("suggestion", "")).strip()
                for item in issues
                if str(item.get("suggestion", "")).strip()
            ][:8],
            "search_queries": search_queries,
            "next_action": next_action,
        }
        state.setdefault("review_history", []).append(review_entry)

        self.add_message(state, "review", {
            "agent": self.name,
            "verdict": "pass" if gate["passed"] else "blocked",
            "model_verdict": gate["model_verdict"],
            "quality_score": state["quality_score"],
            "quality_gate_passed": gate["passed"],
            "quality_gate_reasons": gate["reasons"],
            "issues_count": len(issues),
            "critical_issues": sum(i.get("severity") == "critical" for i in issues),
            "major_issues": sum(i.get("severity") == "major" for i in issues),
            "summary": normalized_result["overall_assessment"].get("summary", ""),
            "missing_aspects": missing_aspects,
            "fact_check_failures": len(fact_check_failures),
            "next_action": next_action,
            "round": review_round,
        })

        for issue in [i for i in issues if i.get("severity") == "critical"][:3]:
            self.add_message(state, "critic_feedback", {
                "agent": self.name,
                "issue_type": issue.get("issue_type"),
                "severity": issue.get("severity"),
                "description": issue.get("description"),
                "suggestion": issue.get("suggestion")
            })

        record_research_event(
            "critic.review_completed",
            phase=state.get("phase"),
            status="info" if gate["passed"] else "warning",
            payload={
                "model_verdict": gate["model_verdict"],
                "quality_score": state["quality_score"],
                "quality_gate_passed": gate["passed"],
                "quality_gate_reasons": gate["reasons"],
                "issues_count": len(issues),
                "fact_check_failures": len(fact_check_failures),
                "review_round": review_round,
                "next_action": next_action,
            },
        )

        return state

    def _evaluate_quality_gate(self, review_result: Any) -> Dict[str, Any]:
        """Normalize an LLM review and enforce the publication gate in code."""

        invalid_response = not isinstance(review_result, dict)
        raw = review_result if isinstance(review_result, dict) else {}
        overall = raw.get("overall_assessment")
        if not isinstance(overall, dict):
            overall = {}
            invalid_response = True

        try:
            quality_score = float(overall.get("quality_score", 0.0))
        except (TypeError, ValueError):
            quality_score = 0.0
            invalid_response = True
        if not 0.0 <= quality_score <= 10.0:
            quality_score = min(max(quality_score, 0.0), 10.0)
            invalid_response = True

        model_verdict = str(overall.get("verdict", "invalid")).strip().lower()
        if model_verdict not in {"pass", "needs_revision", "major_issues"}:
            invalid_response = True

        raw_issues = raw.get("issues")
        if not isinstance(raw_issues, list):
            raw_issues = []
            invalid_response = True
        issues = []
        for item in raw_issues:
            if not isinstance(item, dict):
                invalid_response = True
                continue
            normalized_issue = dict(item)
            issue_type = str(item.get("issue_type", "")).strip().lower()
            severity = str(item.get("severity", "")).strip().lower()
            description = str(item.get("description", "")).strip()
            if (
                issue_type not in self.ALLOWED_ISSUE_TYPES
                or severity not in self.ALLOWED_SEVERITIES
                or not description
            ):
                invalid_response = True
            normalized_issue.update(
                issue_type=issue_type,
                severity=severity,
                description=description,
            )
            issues.append(normalized_issue)

        raw_missing = raw.get("missing_aspects")
        if not isinstance(raw_missing, list):
            raw_missing = []
            invalid_response = True
        missing_aspects = [str(item).strip() for item in raw_missing if str(item).strip()]

        raw_fact_checks = raw.get("fact_check_results")
        if not isinstance(raw_fact_checks, list):
            raw_fact_checks = []
            invalid_response = True
        fact_checks = []
        for item in raw_fact_checks:
            if not isinstance(item, dict):
                invalid_response = True
                continue
            normalized_check = dict(item)
            fact_id = str(item.get("fact_id", "")).strip()
            fact_status = str(item.get("status", "")).strip().lower()
            if not fact_id or fact_status not in self.ALLOWED_FACT_STATUSES:
                invalid_response = True
            normalized_check.update(fact_id=fact_id, status=fact_status)
            fact_checks.append(normalized_check)

        blocking = [
            item for item in issues
            if str(item.get("severity", "")).lower() in self.BLOCKING_SEVERITIES
        ]
        fact_check_failures = [
            item for item in fact_checks
            if item.get("status") in self.BLOCKING_FACT_STATUSES
        ]
        reasons: List[str] = []
        if invalid_response:
            reasons.append("invalid_critic_response")
        if quality_score < self.MIN_PASS_SCORE:
            reasons.append("quality_score_below_threshold")
        if model_verdict != "pass":
            reasons.append("model_verdict_not_pass")
        if blocking:
            reasons.append("blocking_issues_present")
        if missing_aspects:
            reasons.append("missing_aspects_present")
        if fact_check_failures:
            reasons.append("fact_check_failures_present")

        normalized = {
            **raw,
            "overall_assessment": {
                **overall,
                "quality_score": quality_score,
                "verdict": model_verdict,
            },
            "issues": issues,
            "missing_aspects": missing_aspects,
            "fact_check_results": fact_checks,
        }
        return {
            "passed": not reasons,
            "reasons": reasons,
            "invalid_response": invalid_response,
            "quality_score": quality_score,
            "model_verdict": model_verdict,
            "fact_check_failures": fact_check_failures,
            "review_result": normalized,
        }

    def _analyze_issues_for_routing(self, review_result: Dict[str, Any]) -> Dict[str, Any]:
        """
        分析问题类型，决定路由方向

        Returns:
            {
                "should_research": bool,  # 是否需要重新搜索
                "search_queries": List[str]  # 建议的搜索查询
            }
        """
        issues = review_result.get("issues", [])
        missing_aspects = review_result.get("missing_aspects", [])
        fact_check_failures = [
            item for item in review_result.get("fact_check_results", [])
            if item.get("status") in self.BLOCKING_FACT_STATUSES
        ]

        # 需要补充搜索的问题类型
        research_needed_types = {"missing_source", "incomplete", "outdated"}

        search_queries = []
        research_issues_count = 0

        for issue in issues:
            issue_type = issue.get("issue_type", "")
            severity = issue.get("severity", "minor")

            # 检查是否是需要搜索的问题类型
            if issue_type in research_needed_types and severity in ["critical", "major"]:
                research_issues_count += 1

                # 收集搜索建议
                if issue.get("requires_new_search") and issue.get("search_query"):
                    search_queries.append(issue["search_query"])

        # 添加遗漏方面的搜索查询
        for aspect in missing_aspects[:3]:
            search_queries.append(aspect)

        # 未验证或可疑事实必须回到检索阶段补证，不能只在原文上反复润色。
        for item in fact_check_failures[:3]:
            search_queries.append(
                str(item.get("reason") or item.get("fact_id") or "补充事实核验")
            )

        # 决策：如果有超过30%的严重问题需要搜索，或者有明确的搜索建议，则回到搜索阶段
        total_critical_major = len([i for i in issues if i.get("severity") in ["critical", "major"]])
        should_research = (
            len(search_queries) > 0 and
            (
                research_issues_count > 0
                or len(missing_aspects) > 0
                or len(fact_check_failures) > 0
            ) and
            (total_critical_major == 0 or research_issues_count / max(total_critical_major, 1) > 0.3)
        )

        return {
            "should_research": should_research,
            "search_queries": list(set(search_queries))[:5]  # 去重，最多5个查询
        }

    async def _review_content(self, state: ResearchState) -> Dict[str, Any]:
        """审核内容"""
        self.logger.info(f"[CriticMaster] _review_content 开始")

        # 准备草稿内容
        draft_content = ""
        for section_id, content in state["draft_sections"].items():
            section = next((s for s in state["outline"] if s.get("id") == section_id), {})
            draft_content += f"\n## {section.get('title', section_id)}\n{content}\n"

        if not draft_content:
            draft_content = state.get("final_report", "（暂无内容）")

        self.logger.info(f"[CriticMaster] 待审核内容长度: {len(draft_content)}")

        # 准备事实列表
        facts_summary = []
        for fact in state["facts"][:20]:
            facts_summary.append(f"- [{fact.get('id')}] {fact.get('content', '')[:150]} (来源: {fact.get('source_name')}, 可信度: {fact.get('credibility_score')})")

        # 准备数据点列表
        data_summary = []
        for dp in state["data_points"][:15]:
            data_summary.append(f"- {dp.get('name')}: {dp.get('value')} {dp.get('unit', '')} (来源: {dp.get('source')})")

        # 格式化大纲
        outline_summary = []
        for section in state["outline"]:
            outline_summary.append(f"- {section.get('id')}: {section.get('title')} ({section.get('status', 'pending')})")

        prompt = self.REVIEW_PROMPT.format(
            query=state["query"],
            outline="\n".join(outline_summary),
            draft_content=draft_content[:8000],  # 限制长度
            facts="\n".join(facts_summary) if facts_summary else "（暂无事实记录）",
            data_points="\n".join(data_summary) if data_summary else "（暂无数据点）"
        )

        self.logger.info(f"[CriticMaster] 调用 LLM 进行审核...")
        response = await self.call_llm(
            system_prompt="你是一位极其严苛的质量审核专家，专门找出研究报告中的问题。你永远不会轻易满意。",
            user_prompt=prompt,
            json_mode=True,
            temperature=0.2,
            max_tokens=16000  # 拉满到最大值
        )
        self.logger.info(f"[CriticMaster] LLM 响应长度: {len(response)}")

        result = self.parse_json_response(response)
        self.logger.info(f"[CriticMaster] JSON 解析结果: {bool(result)}, verdict: {result.get('overall_assessment', {}).get('verdict') if result else 'N/A'}")
        return result

    async def final_check(self, state: ResearchState) -> Dict[str, Any]:
        """最终检查"""
        # 收集之前的问题
        previous_issues = []
        for issue in state["critic_feedback"]:
            if not issue.get("resolved"):
                previous_issues.append(f"- [{issue.get('severity')}] {issue.get('description')}")

        prompt = self.FINAL_CHECK_PROMPT.format(
            query=state["query"],
            previous_issues="\n".join(previous_issues) if previous_issues else "无之前的问题",
            revised_content=state.get("final_report", "")[:8000]
        )

        response = await self.call_llm(
            system_prompt="你是最终质量把关人。",
            user_prompt=prompt,
            json_mode=True
        )

        return self.parse_json_response(response)
