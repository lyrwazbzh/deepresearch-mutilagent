# Copyright © 2026 深圳市深维智见教育科技有限公司 版权所有
# 未经授权，禁止转售或仿制。

"""
DeepResearch V2.0 - 总架构师 Agent (ChiefArchitect)

职责：
1. 意图解码 - 深度理解用户问题
2. 知识图谱初始化 - 识别关键实体和关系
3. 动态大纲生成 - 创建可执行的研究计划
4. 进度监控 - 根据研究进展动态调整大纲
"""

import json
import uuid
from typing import Dict, Any, List
from datetime import datetime

from .base import BaseAgent
from ..state import ResearchState, ResearchPhase


class ChiefArchitect(BaseAgent):
    """
    总架构师 - 研究规划的大脑

    特点：
    - 动态DAG调度
    - 大局观，能看到全貌
    - 根据新发现调整计划
    """

    PLANNING_PROMPT = """研究课题：{query}

请为该课题生成研究大纲和研究假设，输出JSON格式如下：

{{
  "hypothesis_1": "关于市场/行业趋势的假设（需要验证）",
  "hypothesis_2": "关于竞争格局或技术发展的假设（需要验证）",
  "hypothesis_3": "关于政策或外部因素影响的假设（需要验证）",
  "sec_1_title": "市场概况",
  "sec_1_desc": "描述市场规模、增速",
  "sec_1_query": "搜索关键词",
  "sec_2_title": "竞争格局",
  "sec_2_desc": "描述主要企业",
  "sec_2_query": "搜索关键词",
  "sec_3_title": "技术趋势",
  "sec_3_desc": "描述核心技术",
  "sec_3_query": "搜索关键词",
  "sec_4_title": "政策环境",
  "sec_4_desc": "描述相关政策",
  "sec_4_query": "搜索关键词",
  "sec_5_title": "挑战机遇",
  "sec_5_desc": "描述挑战和机会",
  "sec_5_query": "搜索关键词",
  "sec_6_title": "未来展望",
  "sec_6_desc": "描述发展趋势",
  "sec_6_query": "搜索关键词",
  "questions": "核心问题1;核心问题2;核心问题3"
}}

研究假设示例：
- 假设市场规模将持续增长，需要用数据验证增速
- 假设某类技术会成为主流，需要找证据支持或反驳
- 假设政策变化会影响行业格局，需要分析政策走向

请根据研究课题填写具体内容，每个字段都是字符串类型。"""

    OUTLINE_ONLY_PROMPT = """研究课题：{query}

请生成一份可由用户审核的深度研究计划，严格输出 JSON：
{{
  "sections": [
    {{"id": "section_唯一标识", "title": "章节标题", "description": "研究范围描述"}}
  ],
  "research_questions": [
    {{"id": "question_唯一标识", "text": "需要回答的核心研究问题"}}
  ]
}}

要求：
1. 生成 5-8 个章节，每章标题和描述必须具体且非空。
2. 生成 3-6 个核心研究问题。
3. ID 在本次输出中必须唯一且稳定。
4. 不要生成搜索关键词或研究假设。"""

    SEARCH_PLAN_PROMPT = """研究课题：{query}

用户批准的章节与核心研究问题：
{approved_plan}

请基于最终计划生成每章搜索关键词与可验证研究假设，严格输出 JSON：
{{
  "sections": [
    {{"id": "必须与输入章节ID一致", "queries": ["精准关键词1", "精准关键词2"]}}
  ],
  "hypotheses": [
    {{"id": "hypothesis_唯一标识", "content": "可被证据支持或反驳的假设"}}
  ]
}}

要求：每个输入章节都必须出现一次；不要创造未知章节 ID；每章至少两个非空关键词。{retry_feedback}"""

    REVISION_PROMPT = """你是总架构师，需要根据研究进展动态调整大纲。

## 原始问题
{query}

## 当前大纲
{current_outline}

## 新发现的重要信息
{new_findings}

## 当前进度
- 已完成章节: {completed_sections}
- 收集的事实数量: {facts_count}
- 发现的数据点: {data_points_count}

## 任务
评估是否需要调整大纲。可能的调整包括：
1. 新增章节（发现了重要的新方向）
2. 删除章节（发现某方向信息太少）
3. 调整章节顺序或优先级
4. 细化或合并章节

输出JSON格式：
```json
{{
    "needs_revision": true或false,
    "revision_reason": "调整原因",
    "revised_outline": [...],  // 如果needs_revision为true
    "new_search_queries": ["新增的搜索关键词"]  // 如果需要补充搜索
}}
```"""

    def __init__(self, llm_api_key: str, llm_base_url: str, model: str = "qwen-max"):
        super().__init__(
            name="ChiefArchitect",
            role="总架构师",
            llm_api_key=llm_api_key,
            llm_base_url=llm_base_url,
            model=model
        )

    @staticmethod
    def _with_memory_context(prompt: str, state: ResearchState) -> str:
        context = str(state.get("memory_context", "")).strip()
        if not context:
            return prompt
        return (
            f"{prompt}\n\n可参考的历史记忆（仅作规划上下文，仍需以本次检索证据验证）：\n"
            f"{context[:12000]}"
        )

    def _convert_flat_to_outline(self, flat_result: Dict) -> Dict:
        """将扁平JSON格式转换为标准outline格式"""
        outline = []
        for i in range(1, 10):  # 最多支持9个章节
            title_key = f"sec_{i}_title"
            desc_key = f"sec_{i}_desc"
            query_key = f"sec_{i}_query"

            if title_key not in flat_result:
                break

            section = {
                "id": f"sec_{i}",
                "title": flat_result.get(title_key, f"章节{i}"),
                "description": flat_result.get(desc_key, ""),
                "section_type": "mixed",
                "requires_data": i <= 2,  # 前两章需要数据
                "requires_chart": i <= 2,
                "search_queries": [flat_result.get(query_key, flat_result.get(title_key, ""))]
            }
            outline.append(section)

        # 处理研究问题
        questions_str = flat_result.get("questions", "")
        if isinstance(questions_str, str):
            research_questions = [q.strip() for q in questions_str.split(";") if q.strip()]
        else:
            research_questions = []

        # 处理研究假设（假设驱动研究）
        hypotheses = []
        for i in range(1, 6):  # 最多5个假设
            h_key = f"hypothesis_{i}"
            if h_key in flat_result and flat_result[h_key]:
                hypotheses.append({
                    "id": f"h_{i}",
                    "content": flat_result[h_key],
                    "status": "unverified",  # unverified, supported, refuted, partially_supported
                    "evidence_for": [],
                    "evidence_against": []
                })

        return {
            "outline": outline,
            "research_questions": research_questions,
            "hypotheses": hypotheses,
            "key_entities": []
        }

    async def process(self, state: ResearchState) -> ResearchState:
        """
        处理入口

        根据当前阶段执行不同的规划任务
        """
        if state["phase"] == ResearchPhase.INIT.value:
            return await self._generate_outline(state)
        elif state["phase"] == ResearchPhase.PLANNING.value:
            return await self._generate_search_plan(state)
        elif state["phase"] == ResearchPhase.REVIEWING.value:
            return await self._check_revision(state)
        else:
            return state

    async def _generate_outline(self, state: ResearchState) -> ResearchState:
        """生成等待用户审核的章节与核心研究问题。"""
        self.add_message(state, "research_step", {
            "step_id": f"step_planning_{uuid.uuid4().hex[:8]}",
            "step_type": "planning",
            "title": "研究计划",
            "subtitle": "生成待审核大纲",
            "status": "running",
            "stats": {},
        })
        self.add_message(state, "thought", {
            "agent": self.name,
            "content": "正在生成可审核的研究章节与核心问题...",
        })

        prompt = self._with_memory_context(
            self.OUTLINE_ONLY_PROMPT.format(query=state["query"]), state
        )
        normalized_sections = []
        normalized_questions = []

        for _attempt in range(3):
            response = await self.call_llm(
                system_prompt="你是一位专业的行业研究规划师，只输出合法 JSON。",
                user_prompt=prompt,
                json_mode=True,
                temperature=0.3,
                max_tokens=8000,
            )
            result = self.parse_json_response(response) or {}
            normalized_sections = self._normalize_outline_sections(
                result.get("sections", [])
            )
            normalized_questions = self._normalize_research_questions(
                result.get("research_questions", [])
            )
            if 5 <= len(normalized_sections) <= 8 and 3 <= len(normalized_questions) <= 6:
                break

        if not (5 <= len(normalized_sections) <= 8 and 3 <= len(normalized_questions) <= 6):
            state["errors"].append("大纲生成失败：章节或核心研究问题数量不合格")
            self.add_message(state, "planning_error", {
                "stage": "outline_generation",
                "message": state["errors"][-1],
                "retryable": True,
            })
            return state

        state["outline"] = normalized_sections
        state["research_questions"] = normalized_questions
        state["hypotheses"] = []
        state["outline_revision"] = uuid.uuid4().hex
        state["phase"] = ResearchPhase.AWAITING_OUTLINE_APPROVAL.value

        self.add_message(state, "outline", {
            "outline": normalized_sections,
            "research_questions": normalized_questions,
            "outline_revision": state["outline_revision"],
        })
        self.add_message(state, "research_step", {
            "step_type": "planning",
            "title": "研究计划",
            "subtitle": "等待用户审核",
            "status": "paused",
            "stats": {
                "sections_count": len(normalized_sections),
                "questions_count": len(normalized_questions),
            },
        })
        return state

    def _normalize_outline_sections(self, sections: Any) -> List[Dict[str, Any]]:
        if not isinstance(sections, list):
            return []

        normalized = []
        used_ids = set()
        for index, section in enumerate(sections):
            if not isinstance(section, dict):
                continue
            title = str(section.get("title", "")).strip()
            description = str(section.get("description", "")).strip()
            if not title or not description:
                continue
            section_id = str(section.get("id", "")).strip()
            if not section_id or section_id in used_ids:
                section_id = f"section_{uuid.uuid4().hex}"
            used_ids.add(section_id)
            normalized.append({
                "id": section_id,
                "title": title,
                "description": description,
                "section_type": section.get("section_type", "mixed"),
                "requires_data": bool(section.get("requires_data", index < 2)),
                "requires_chart": bool(section.get("requires_chart", index < 2)),
                "priority": index + 1,
                "search_queries": [],
                "status": "pending",
            })
        return normalized

    def _normalize_research_questions(self, questions: Any) -> List[Dict[str, str]]:
        if not isinstance(questions, list):
            return []

        normalized = []
        used_ids = set()
        for question in questions:
            if isinstance(question, str):
                question = {"text": question}
            if not isinstance(question, dict):
                continue
            text = str(question.get("text", "")).strip()
            if not text:
                continue
            question_id = str(question.get("id", "")).strip()
            if not question_id or question_id in used_ids:
                question_id = f"question_{uuid.uuid4().hex}"
            used_ids.add(question_id)
            normalized.append({"id": question_id, "text": text})
        return normalized

    async def _generate_search_plan(self, state: ResearchState) -> ResearchState:
        """基于用户批准的计划生成关键词与研究假设。"""
        outline = state.get("outline", [])
        approved_plan = json.dumps({
            "sections": outline,
            "research_questions": state.get("research_questions", []),
        }, ensure_ascii=False)
        expected_ids = {section.get("id") for section in outline}
        valid_queries = {}
        hypotheses = []
        retry_feedback = ""

        for attempt in range(2):
            prompt = self.SEARCH_PLAN_PROMPT.format(
                query=state["query"],
                approved_plan=approved_plan,
                retry_feedback=retry_feedback,
            )
            prompt = self._with_memory_context(prompt, state)
            response = await self.call_llm(
                system_prompt="你是一位专业的行业研究检索规划师，只输出合法 JSON。",
                user_prompt=prompt,
                json_mode=True,
                temperature=0.3,
                max_tokens=8000,
            )
            result = self.parse_json_response(response) or {}
            valid_queries = self._parse_section_queries(
                result.get("sections", []), expected_ids
            )
            hypotheses = self._normalize_hypotheses(result.get("hypotheses", []))
            if len(valid_queries) == len(expected_ids):
                break
            retry_feedback = (
                f"上次输出仅覆盖 {len(valid_queries)}/{len(expected_ids)} 个有效章节，"
                "请修正缺失、重复或未知 ID。"
            )

        if len(valid_queries) < 3:
            state["errors"].append(
                f"关键词生成失败：仅 {len(valid_queries)} 个章节获得有效关键词"
            )
            self.add_message(state, "planning_error", {
                "stage": "keyword_generation",
                "message": state["errors"][-1],
                "retryable": True,
            })
            return state

        fallback_ids = []
        for section in outline:
            section_id = section.get("id")
            queries = valid_queries.get(section_id)
            if not queries:
                fallback_ids.append(section_id)
                queries = [
                    f"{state['query']} {section.get('title', '')}".strip(),
                    f"{section.get('title', '')} {section.get('description', '')}".strip(),
                ]
            section["search_queries"] = queries

        state["outline"] = outline
        state["hypotheses"] = hypotheses
        state["phase"] = ResearchPhase.RESEARCHING.value
        self.add_message(state, "keywords_generated", {
            "sections_count": len(outline),
            "fallback_section_ids": fallback_ids,
            "hypotheses_count": len(hypotheses),
        })
        return state

    def _parse_section_queries(
        self,
        sections: Any,
        expected_ids: set,
    ) -> Dict[str, List[str]]:
        if not isinstance(sections, list):
            return {}

        parsed = {}
        duplicate_ids = set()
        for section in sections:
            if not isinstance(section, dict):
                continue
            section_id = section.get("id")
            if section_id not in expected_ids:
                continue
            if section_id in parsed:
                duplicate_ids.add(section_id)
                continue
            queries = section.get("queries", [])
            if not isinstance(queries, list):
                continue
            cleaned = [str(query).strip() for query in queries if str(query).strip()]
            if cleaned:
                parsed[section_id] = list(dict.fromkeys(cleaned))
        for section_id in duplicate_ids:
            parsed.pop(section_id, None)
        return parsed

    def _normalize_hypotheses(self, hypotheses: Any) -> List[Dict[str, Any]]:
        if not isinstance(hypotheses, list):
            return []

        normalized = []
        used_ids = set()
        for hypothesis in hypotheses:
            if isinstance(hypothesis, str):
                hypothesis = {"content": hypothesis}
            if not isinstance(hypothesis, dict):
                continue
            content = str(hypothesis.get("content", "")).strip()
            if not content:
                continue
            hypothesis_id = str(hypothesis.get("id", "")).strip()
            if not hypothesis_id or hypothesis_id in used_ids:
                hypothesis_id = f"hypothesis_{uuid.uuid4().hex}"
            used_ids.add(hypothesis_id)
            normalized.append({
                "id": hypothesis_id,
                "content": content,
                "status": "unverified",
                "evidence_for": [],
                "evidence_against": [],
            })
        return normalized

    async def _initial_planning(self, state: ResearchState) -> ResearchState:
        """初始规划"""
        self.logger.info(f"Starting initial planning for: {state['query'][:50]}...")

        # 发送 research_step 开始事件
        self.add_message(state, "research_step", {
            "step_id": f"step_planning_{uuid.uuid4().hex[:8]}",
            "step_type": "planning",
            "title": "研究计划",
            "subtitle": "分析问题，制定大纲",
            "status": "running",
            "stats": {}
        })

        # 发送状态消息
        self.add_message(state, "thought", {
            "agent": self.name,
            "content": "正在分析研究问题，构建知识图谱和研究大纲..."
        })

        # 调用LLM生成规划 - 带重试机制
        prompt = self._with_memory_context(
            self.PLANNING_PROMPT.format(query=state["query"]), state
        )
        result = None
        max_retries = 2

        for attempt in range(max_retries + 1):
            response = await self.call_llm(
                system_prompt="你是一位专业的行业研究规划师。请严格按照要求的JSON格式输出，不要添加任何额外内容。",
                user_prompt=prompt,
                json_mode=True,
                temperature=0.3,
                max_tokens=16000  # 拉满到最大值
            )

            # Debug: 记录原始响应
            self.logger.info(f"Raw LLM response length: {len(response)} (attempt {attempt + 1})")
            self.logger.debug(f"Raw LLM response (first 1000 chars): {response[:1000]}")

            result = self.parse_json_response(response)

            # 检查是否是扁平格式，需要转换
            if result and result.get("sec_1_title") and not result.get("outline"):
                result = self._convert_flat_to_outline(result)

            if result and result.get("outline") and len(result.get("outline", [])) >= 3:
                self.logger.info(f"Successfully parsed outline with {len(result['outline'])} sections")
                break

            # 诊断失败原因
            if not result:
                self.logger.warning(f"Attempt {attempt + 1}: JSON parsing failed completely")
            elif not result.get("outline"):
                self.logger.warning(f"Attempt {attempt + 1}: No 'outline' key in result. Keys: {list(result.keys())}")
            elif len(result.get("outline", [])) < 3:
                self.logger.warning(f"Attempt {attempt + 1}: Outline too short: {len(result.get('outline', []))} sections")

            if attempt < max_retries:
                self.logger.warning(f"Outline generation failed or incomplete, retrying... (attempt {attempt + 1})")
                # 简化提示词重试
                prompt = f"""请为"{state['query']}"生成研究大纲。

输出JSON格式：
{{"outline": [
    {{"id": "sec_1", "title": "章节标题", "description": "描述", "section_type": "mixed", "requires_data": true, "requires_chart": false, "search_queries": ["关键词1", "关键词2"]}},
    ...更多章节(共5-8个)...
], "research_questions": ["问题1", "问题2", "问题3"], "key_entities": []}}

要求：outline必须包含5-8个章节，覆盖市场概况、企业竞争、技术趋势、政策环境、未来展望等方面。"""

        if not result:
            state["errors"].append("Failed to generate research plan after retries")
            self.logger.error(f"Raw LLM response: {response[:800]}")
            return state

        # Debug: log outline count
        self.logger.info(f"Parsed result keys: {list(result.keys())}")
        outline = result.get("outline", [])
        self.logger.info(f"Outline in result: {len(outline)} sections")
        if not outline:
            self.logger.warning(f"No outline found! Full parsed result: {str(result)[:500]}")

        # 更新状态
        state["key_entities"] = [e.get("name", "") for e in result.get("key_entities", []) if isinstance(e, dict)]
        state["mind_map"] = result.get("mind_map", {})
        state["research_questions"] = result.get("research_questions", [])
        state["hypotheses"] = result.get("hypotheses", [])  # 假设驱动研究
        state["knowledge_graph"] = {"nodes": [], "edges": []}  # 知识图谱初始化

        # 处理大纲 - 确保每个章节都有必要字段
        processed_outline = []
        for i, section in enumerate(outline):
            if not isinstance(section, dict):
                continue
            processed_section = {
                "id": section.get("id", f"sec_{i+1}"),
                "title": section.get("title", f"章节{i+1}"),
                "description": section.get("description", ""),
                "section_type": section.get("section_type", "mixed"),
                "requires_data": section.get("requires_data", False),
                "requires_chart": section.get("requires_chart", False),
                "priority": section.get("priority", i+1),
                "search_queries": section.get("search_queries", [section.get("title", "")]),
                "status": "pending"
            }
            # 确保 search_queries 是列表且非空
            if not isinstance(processed_section["search_queries"], list):
                processed_section["search_queries"] = [str(processed_section["search_queries"])]
            # 过滤空字符串，如果结果为空则使用章节标题
            processed_section["search_queries"] = [q for q in processed_section["search_queries"] if q and q.strip()]
            if not processed_section["search_queries"]:
                processed_section["search_queries"] = [processed_section["title"]]
            processed_outline.append(processed_section)

        state["outline"] = processed_outline
        self.logger.info(f"Processed outline: {len(processed_outline)} sections")

        # 发送大纲事件
        self.add_message(state, "outline", {
            "understanding": result.get("understanding", {}),
            "key_entities": result.get("key_entities", []),
            "outline": outline,
            "research_questions": state["research_questions"]
        })

        # 更新阶段
        state["phase"] = ResearchPhase.PLANNING.value

        # 发送 research_step 完成事件
        self.add_message(state, "research_step", {
            "step_type": "planning",
            "title": "研究计划",
            "subtitle": "分析问题，制定大纲",
            "status": "completed",
            "stats": {
                "sections_count": len(processed_outline),
                "questions_count": len(state["research_questions"])
            }
        })

        self.logger.info(f"Planning completed. Generated {len(outline)} sections.")

        return state

    async def _check_revision(self, state: ResearchState) -> ResearchState:
        """检查是否需要修订大纲"""
        # 收集新发现
        new_findings = []
        for fact in state["facts"][-10:]:  # 最近10条事实
            new_findings.append(f"- {fact.get('content', '')[:100]}")

        if not new_findings:
            return state

        # 统计进度
        completed = [s for s in state["outline"] if s.get("status") == "final"]

        prompt = self.REVISION_PROMPT.format(
            query=state["query"],
            current_outline=state["outline"],
            new_findings="\n".join(new_findings),
            completed_sections=len(completed),
            facts_count=len(state["facts"]),
            data_points_count=len(state["data_points"])
        )

        response = await self.call_llm(
            system_prompt="你是总架构师，需要判断是否需要调整研究计划。",
            user_prompt=prompt,
            json_mode=True
        )

        result = self.parse_json_response(response)

        if result.get("needs_revision") and result.get("revised_outline"):
            state["outline"] = result["revised_outline"]
            self.add_message(state, "outline_revision", {
                "reason": result.get("revision_reason"),
                "new_outline": result["revised_outline"]
            })
            self.logger.info(f"Outline revised: {result.get('revision_reason')}")

        return state
