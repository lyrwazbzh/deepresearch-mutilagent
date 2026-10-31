# Copyright © 2026 深圳市深维智见教育科技有限公司 版权所有
# 未经授权，禁止转售或仿制。

"""DeepResearch V2.0 asynchronous research state machine.

The hand-written asyncio runtime owns SSE streaming, checkpointing,
cancellation and review/revision orchestration.
"""

import logging
import asyncio
import os
from time import perf_counter
from typing import Dict, Any, AsyncGenerator
from datetime import datetime, timedelta, timezone

from observability.events import record_research_event
from observability.context import current_context
from observability.tracing import span
from service.research_memory.types import MemoryQuery
from contracts.tool_routing import CapabilitySet, ToolName, ToolRoutingRequest

# 取消协议的中立位（T68）。此前这里是「反向依赖 router 拿取消函数」的 import，
# 并用 `except ImportError` 把取消**静默降级为「永不取消」**（fail-open）—— 导入一失败，
# 流水线就会静默跑到天亮，而这个兜底同时把两模块间的循环依赖一并掩盖。现在协议归
# core/research_cancel.py（与 router / service 都无关），并且**没有兜底**：该模块
# 不可用 = 本模块导入失败（loud），不再伪装成「未取消」。
# 判定本身则作为**显式注入的协作者**使用 —— 见 __init__ 的 `cancellation`。
from core import research_cancel
from core.deadline import deadline_scope

from .state import ResearchState, ResearchPhase, create_initial_state
from .phase_dispatch import phases_to_run
from .agents import ChiefArchitect, DeepScout, CodeWizard, CriticMaster, LeadWriter, DataAnalyst

# 导入检查点服务
try:
    from service.checkpoint_service import get_checkpoint_service
except ImportError:
    try:
        from app.service.checkpoint_service import get_checkpoint_service
    except ImportError:
        # 兼容直接运行脚本的情况
        def get_checkpoint_service():
            return None

# 导入配置
try:
    from config.llm_config import get_config
except ImportError:
    try:
        from app.config.llm_config import get_config
    except ImportError:
        # 兼容直接运行脚本的情况
        import sys
        import os
        sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
        from config.llm_config import get_config

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
logger = logging.getLogger("DeepResearchGraph")


class DeepResearchGraph:
    """
    DeepResearch V2.0 工作流图

    实现完整的多智能体协作流程：
    1. Plan (ChiefArchitect) - 分析问题，生成研究大纲
    2. Research (DeepScout) - 并行深度搜索
    3. Analyze (CodeWizard) - 数据分析和可视化
    4. Write (LeadWriter) - 撰写报告
    5. Review (CriticMaster) - 对抗式审核
    6. Revise (LeadWriter) - 修订（如果需要）
    """

    def __init__(
        self,
        llm_api_key: str = None,
        llm_base_url: str = None,
        search_api_key: str = None,
        model: str = None,
        max_iterations: int = None,
        *,
        agents: Dict[str, Any] = None,
        checkpoint_service: Any = None,
        cancellation: Any = None,
        memory_service: Any = None,
    ):
        """
        初始化工作流

        所有参数都可从配置文件读取，传入的参数会覆盖配置。

        构造缝（测试注入协作者的入口）：
        - `agents`：六个 agent 的**完整**映射，键为 architect / scout / data_analyst /
          wizard / critic / writer。给了它就**不再读配置、不再自建 agent** ——
          测试因此可以直接走构造器，不必 `object.__new__` 绕过。
        - `checkpoint_service`：检查点服务；仅在注入 `agents` 时生效。
        - `cancellation`：取消判定协作者（需提供 `is_cancelled(session_id) -> bool` 与
          `clear_cancel_flag(session_id)`）。默认走中立模块 `core/research_cancel`
          （Redis 实现）。T68 之前「取消」只有一个模块级 import ＋ `ImportError` 兜底，
          没有任何接缝能让测试注入「已取消」—— 也就意味着「取消真的生效」当时
          **不可判定**（全仓没有任何用例证明过它会让流水线停下）。
        """
        if agents is None:
            # 获取配置
            config = get_config()

            # 使用传入参数或配置默认值
            self.llm_api_key = llm_api_key or config.api_key
            self.llm_base_url = llm_base_url or config.base_url
            self.search_api_key = search_api_key or config.search_api_key
            self.model = model or config.default_model
            self.max_iterations = max_iterations or config.research.max_iterations

            # 初始化各个 Agent（使用各自配置的模型）
            self.architect = ChiefArchitect(
                self.llm_api_key, self.llm_base_url,
                config.agents.architect.model
            )
            self.scout = DeepScout(
                self.llm_api_key, self.llm_base_url, self.search_api_key,
                config.agents.scout.model
            )
            self.data_analyst = DataAnalyst(
                self.llm_api_key, self.llm_base_url,
                config.agents.data_analyst.model
            )
            self.wizard = CodeWizard(
                self.llm_api_key, self.llm_base_url,
                config.agents.wizard.model
            )
            self.critic = CriticMaster(
                self.llm_api_key, self.llm_base_url,
                config.agents.critic.model
            )
            self.writer = LeadWriter(
                self.llm_api_key, self.llm_base_url,
                config.agents.writer.model
            )

            logger.info("DeepResearchGraph initialized with models:")
            logger.info(f"  - Architect: {config.agents.architect.model}")
            logger.info(f"  - Scout: {config.agents.scout.model}")
            logger.info(f"  - DataAnalyst: {config.agents.data_analyst.model}")
            logger.info(f"  - Wizard: {config.agents.wizard.model}")
            logger.info(f"  - Critic: {config.agents.critic.model}")
            logger.info(f"  - Writer: {config.agents.writer.model}")

            # 检查点服务
            self.checkpoint_service = get_checkpoint_service()
        else:
            # 注入路径：协作者全部由调用方提供，不读配置；密钥/模型参数按传入值（可为 None）
            self.llm_api_key = llm_api_key
            self.llm_base_url = llm_base_url
            self.search_api_key = search_api_key
            self.model = model
            self.max_iterations = max_iterations
            for role in ("architect", "scout", "data_analyst", "wizard", "critic", "writer"):
                setattr(self, role, agents[role])
            self.checkpoint_service = checkpoint_service

        # 取消判定协作者（T68）：默认走中立模块（Redis 实现），测试可注入替身。
        # 调用点**不**吞掉协作者的异常 —— 取消不可用必须显式可见，不得静默降级为
        # 「未取消」（那正是迁出前 ImportError 兜底的失败方式）。
        self._cancellation = (
            cancellation if cancellation is not None else research_cancel
        )
        if memory_service is not None:
            self.memory_service = memory_service
        elif agents is None:
            from service.research_memory.factory import get_research_memory_service
            self.memory_service = get_research_memory_service()
        else:
            # 构造缝测试不隐式连接数据库/向量库；需要验证记忆时显式注入替身。
            self.memory_service = None

    def _save_checkpoint(
        self,
        state: Dict[str, Any],
        user_id: str = None,
        ui_state: Dict[str, Any] = None,
        status: str = "running",
    ) -> bool:
        """保存检查点（包含后端状态和 UI 状态）"""
        if not self.checkpoint_service:
            return False

        session_id = state.get("session_id", "")
        if not session_id:
            return False

        try:
            checkpoint_id = self.checkpoint_service.save_checkpoint(
                session_id=session_id,
                state=state,
                user_id=user_id,
                ui_state=ui_state,
                final_report=state.get("final_report"),
                status=status,
            )
            if checkpoint_id:
                logger.info(f"Checkpoint saved: {checkpoint_id}")
                return True
        except Exception as e:
            logger.warning(f"Failed to save checkpoint: {e}")

        return False

    def _load_checkpoint(
        self,
        session_id: str,
        user_id: str = None,
    ) -> Dict[str, Any]:
        """加载检查点"""
        if not self.checkpoint_service:
            return None

        try:
            state = self.checkpoint_service.load_checkpoint(
                session_id,
                user_id=user_id,
            )
            if state:
                logger.info(f"Checkpoint loaded for session: {session_id}")
                return state
        except Exception as e:
            logger.warning(f"Failed to load checkpoint: {e}")

        return None

    def get_checkpoint_info(self, session_id: str) -> Dict[str, Any]:
        """获取检查点信息"""
        if not self.checkpoint_service:
            return None
        return self.checkpoint_service.get_checkpoint_info(session_id)

    async def _route_analysis(self, state: ResearchState) -> set[ToolName]:
        """Select analysis agents through the same versioned routing contract."""
        from service.tool_routing import get_tool_router

        context = current_context()
        request = ToolRoutingRequest(
            run_id=(context.run_id if context and context.run_id else state["session_id"]),
            session_id=state["session_id"],
            stage="analysis",
            query=state["query"],
            context_summary=(
                f"facts={len(state.get('facts', []))};"
                f"data_points={len(state.get('data_points', []))};"
                f"chart_required={any(item.get('requires_chart') for item in state.get('outline', []))}"
            ),
            allowed_tools=[ToolName.DATA_ANALYSIS, ToolName.CHART_GENERATION],
            capabilities=CapabilitySet(),
        )
        decision = await get_tool_router().route(request)
        decision.ensure_allowed(request)
        state.setdefault("tool_decisions", []).append(decision.model_dump(mode="json"))
        state.setdefault("research_tasks", []).extend(
            {
                "task_id": call.call_id,
                "query": state["query"],
                "tool": call.name.value,
                "arguments": call.arguments,
            }
            for call in decision.calls
        )
        return {call.name for call in decision.calls}

    async def run(
        self,
        query: str,
        session_id: str,
        resume: bool = False,
        user_id: str = None,
        search_web: bool = True,
        search_local: bool = False,
        kb_name: str = None,
        search_database: bool = False,
        database_profile_id: str = None,
    ) -> AsyncGenerator[Dict[str, Any], None]:
        """
        执行研究流程（流式输出）

        Args:
            query: 用户问题
            session_id: 会话ID
            resume: 是否从检查点恢复
            user_id: 用户ID（用于检查点）
            search_web: 是否启用网络搜索（默认True）
            search_local: 是否启用本地知识库搜索（默认False）
            kb_name: 本地知识库名称（search_local=True 时应提供）

        Yields:
            SSE 事件字典
        """
        # 尝试从检查点恢复
        state = None
        if resume and session_id:
            state = self._load_checkpoint(session_id, user_id=user_id)
            if state:
                yield {
                    "type": "research_resumed",
                    "phase": state.get("phase", ""),
                    "session_id": session_id,
                    "timestamp": datetime.now().isoformat()
                }

        # 如果没有检查点，创建初始状态
        if not state:
            state = create_initial_state(
                query, session_id,
                search_web=search_web,
                search_local=search_local,
                kb_name=kb_name,
                search_database=search_database,
                database_profile_id=database_profile_id,
            )
            state["max_iterations"] = self.max_iterations

            yield {
                "type": "research_start",
                "query": query,
                "session_id": session_id,
                "search_web": search_web,
                "search_local": search_local,
                "kb_name": kb_name,
                "search_database": search_database,
                "database_profile_id": database_profile_id,
                "timestamp": datetime.now().isoformat()
            }

        # 存储 user_id 用于检查点
        state["_user_id"] = user_id
        run_deadline = datetime.now(timezone.utc) + timedelta(
            seconds=float(os.getenv("RESEARCH_TIMEOUT_SECONDS", "1800"))
        )

        # 规划前只召回 active 的 Fact/Experience。记忆是辅助上下文，不替代本次证据，
        # 因此检索故障只记录审计事件，不阻断主研究链路。
        if user_id and self.memory_service is not None and not state.get("memory_context"):
            try:
                with deadline_scope(run_deadline):
                    async with asyncio.timeout(
                        float(os.getenv("MEMORY_TIMEOUT_SECONDS", "10"))
                    ):
                        memory_hits = await self.memory_service.retrieve(MemoryQuery(
                            user_id=user_id,
                            query=query,
                            kinds=["fact", "experience"],
                            top_k=6,
                            min_confidence=0.55,
                        ))
                state["memory_context"] = self.memory_service.build_planning_context(memory_hits)
                record_research_event(
                    "memory.retrieved",
                    phase=state.get("phase"),
                    payload={"hit_count": len(memory_hits)},
                )
                if memory_hits:
                    yield {
                        "type": "memory_retrieved",
                        "count": len(memory_hits),
                        "timestamp": datetime.now().isoformat(),
                    }
            except Exception as exc:
                logger.warning("Research-memory retrieval failed: %s", exc)
                record_research_event(
                    "memory.retrieval_failed",
                    phase=state.get("phase"),
                    payload={"error_type": type(exc).__name__},
                )

        with deadline_scope(run_deadline):
            async for event in self._run_state_machine(state):
                yield event

    async def _run_state_machine(self, state: ResearchState) -> AsyncGenerator[Dict[str, Any], None]:
        """
        主执行流程：手写 AsyncIO 状态机

        使用 asyncio.Queue 实现实时流式输出
        """
        # 创建消息队列用于实时输出
        message_queue = asyncio.Queue()
        state["_message_queue"] = message_queue

        # 在飞的 agent 任务（T61 / P-16）：SSE 客户端断连时 Starlette 会关掉本
        # 生成器（在 yield 处抛 GeneratorExit），若不在此处取消这些任务，
        # `asyncio.create_task` 出来的 agent 会**脱管继续跑** —— 归档日志中两处实例
        # 分别续跑 6m42s / 8m58s，而队列已被 finally 置空，于是它的每条事件都打到
        # `[SSE] No queue available` 上，事件整体丢失。
        active_agent_tasks: set = set()

        # 获取 session_id 用于取消检查
        session_id = state.get("session_id", "")

        # 清除之前的取消标志
        if session_id:
            self._cancellation.clear_cancel_flag(session_id)

        async def check_cancelled():
            """检查是否已取消"""
            if session_id and self._cancellation.is_cancelled(session_id):
                return True
            return False

        async def run_agent_with_streaming(agent):
            """执行 agent 并实时 yield 消息"""
            # 检查是否已取消
            if await check_cancelled():
                logger.info(f"Research cancelled before starting agent: {agent.name}")
                return

            logger.info(f"Starting agent: {agent.name}")

            # 启动 agent 处理任务
            async def execute_agent():
                started_at = perf_counter()
                agent_role = getattr(agent, "role", agent.__class__.__name__)
                record_research_event(
                    "agent.started",
                    phase=state.get("phase"),
                    payload={"agent": agent.name, "role": agent_role},
                )
                outcome = "success"
                try:
                    with span(
                        f"agent.{agent.name.lower()}",
                        kind="agent",
                        attributes={"agent": agent.name, "role": agent_role},
                    ) as observation:
                        result = await agent.process(state)
                        observation.update(
                            output={
                                "phase": state.get("phase"),
                                "fact_count": len(state.get("facts", [])),
                                "chart_count": len(state.get("charts", [])),
                            }
                        )
                        return result
                except Exception as exc:
                    outcome = "failure"
                    record_research_event(
                        "agent.failed",
                        phase=state.get("phase"),
                        status="error",
                        payload={"agent": agent.name, "error_type": type(exc).__name__},
                        duration_ms=round((perf_counter() - started_at) * 1000),
                    )
                    raise
                finally:
                    record_research_event(
                        "agent.finished",
                        phase=state.get("phase"),
                        status="error" if outcome == "failure" else "info",
                        payload={"agent": agent.name, "outcome": outcome},
                        duration_ms=round((perf_counter() - started_at) * 1000),
                    )

            task = asyncio.create_task(execute_agent())
            active_agent_tasks.add(task)
            task.add_done_callback(active_agent_tasks.discard)

            msg_count = 0
            # 在任务执行期间持续从队列获取消息
            while not task.done():
                # 定期检查是否已取消
                if await check_cancelled():
                    logger.info(f"Research cancelled during agent: {agent.name}")
                    task.cancel()
                    try:
                        await task
                    except asyncio.CancelledError:
                        pass
                    return

                try:
                    msg = await asyncio.wait_for(message_queue.get(), timeout=0.5)
                    msg_count += 1
                    msg_type = msg.get('type', 'unknown')
                    logger.info(f"[SSE YIELD] [{agent.name}] #{msg_count}: {msg_type}")
                    yield msg
                except asyncio.TimeoutError:
                    # 继续等待，不发送心跳（SSE连接由前端保持）
                    continue
                except Exception as e:
                    logger.warning(f"[{agent.name}] Queue error: {e}")
                    continue

            # 等待任务完成（获取可能的异常）
            try:
                await task
            except Exception as e:
                logger.error(f"Agent {agent.name} error: {e}")

            # 清空剩余的消息
            remaining = 0
            while not message_queue.empty():
                try:
                    msg = message_queue.get_nowait()
                    remaining += 1
                    yield msg
                except Exception:
                    # 队列已取空（asyncio.QueueEmpty），属正常退出路径，仅 debug 记录
                    logger.debug(f"[queue] agent messages drained: {remaining} remaining")
                    break

            logger.info(f"Agent {agent.name} completed. Messages: {msg_count} during, {remaining} remaining")

        # 获取 user_id 用于检查点
        user_id = state.get("_user_id")

        # UI 状态跟踪（用于前端恢复）
        ui_state = {
            "research_steps": [],  # 研究步骤列表
            "search_results": [],  # 搜索结果
            "charts": [],  # 图表数据
            "knowledge_graph": None,  # 知识图谱
            "streaming_report": "",  # 流式报告内容
        }

        def update_ui_state():
            """更新 UI 状态 - 保留已有数据，只有新数据才更新"""
            # 从 state 中同步数据到 ui_state - 只有当新数据有效时才更新
            new_charts = state.get("charts", [])
            if new_charts:
                ui_state["charts"] = new_charts

            new_report = state.get("final_report", "")
            if new_report:
                ui_state["streaming_report"] = new_report

            # 知识图谱 - 只有当有节点或边时才更新
            new_kg = state.get("knowledge_graph", {})
            if new_kg and (new_kg.get("nodes") or new_kg.get("edges")):
                ui_state["knowledge_graph"] = new_kg
            elif not ui_state.get("knowledge_graph"):
                ui_state["knowledge_graph"] = {"nodes": [], "edges": []}

            # 提取搜索结果 - 从 facts 中构建 UI 友好的搜索结果
            facts = state.get("facts", [])
            if facts:
                search_results_for_ui = []
                for fact in facts:
                    # 优先使用 source_name，否则从 content 中提取标题
                    source_name = fact.get("source_name", "")
                    content = fact.get("content", "")
                    # 如果没有 source_name，用 content 的前50个字符作为标题
                    title = source_name if source_name else (content[:50] + "..." if len(content) > 50 else content)
                    search_results_for_ui.append({
                        "id": fact.get("id", ""),
                        "title": title,
                        "source": fact.get("source_type", "web"),
                        "url": fact.get("source_url", ""),
                        "snippet": content[:200] if content else "",
                        "date": fact.get("timestamp", ""),
                    })
                ui_state["search_results"] = search_results_for_ui

            # 构建前端友好的 references - 确保有 title 和 link 字段
            raw_references = state.get("references", [])
            ui_references = []
            for idx, ref in enumerate(raw_references):
                # 从 facts 中查找对应的详细信息
                fact = next((f for f in facts if f.get("source_url") == ref.get("url")), None)
                # 确定标题：优先用 source/marker，否则用 fact 的内容
                title = ref.get("source") or ref.get("marker") or ""
                if not title and fact:
                    content = fact.get("content", "")
                    title = content[:50] + "..." if len(content) > 50 else content
                if not title:
                    title = f"来源 {idx + 1}"

                ui_references.append({
                    "id": ref.get("id", idx + 1),
                    "title": title,
                    "link": ref.get("url", ""),
                    "content": fact.get("content", "")[:200] if fact else "",
                    "source": "web"
                })
            ui_state["references"] = ui_references

            # 打印详细日志
            kg = ui_state.get("knowledge_graph", {})
            logger.info(f"[UI状态更新] charts={len(ui_state.get('charts', []))}, "
                       f"search_results={len(ui_state.get('search_results', []))}, "
                       f"knowledge_graph nodes={len(kg.get('nodes', []) if kg else [])}, "
                       f"knowledge_graph edges={len(kg.get('edges', []) if kg else [])}, "
                       f"references={len(ui_state.get('references', []))}, "
                       f"report_len={len(ui_state.get('streaming_report', ''))}")

        async def save_checkpoint_async(
            step_info: dict = None,
            status: str = "running",
        ):
            """异步保存检查点"""
            # 更新 UI 状态
            update_ui_state()
            # 添加研究步骤
            if step_info:
                # 检查是否已有该步骤，更新状态
                existing = next(
                    (s for s in ui_state["research_steps"] if s.get("type") == step_info.get("type")),
                    None
                )
                if existing:
                    existing.update(step_info)
                    logger.info(f"[检查点] 更新步骤: {step_info.get('type')}, status={step_info.get('status')}")
                else:
                    ui_state["research_steps"].append(step_info)
                    logger.info(f"[检查点] 添加步骤: {step_info.get('type')}, status={step_info.get('status')}")

            # 打印保存前的完整状态
            logger.info(f"[检查点保存] session_id={session_id}, phase={state.get('phase', '')}, "
                       f"steps={[s.get('type') for s in ui_state['research_steps']]}")

            if self._save_checkpoint(state, user_id, ui_state, status=status):
                logger.info(f"[检查点保存成功] session_id={session_id}")
                return {"type": "checkpoint_saved", "phase": state.get("phase", ""), "session_id": session_id}
            else:
                logger.error(f"[检查点保存失败] session_id={session_id}")
            return None

        try:
            execution_phases = phases_to_run(
                state.get("phase", ResearchPhase.INIT.value)
            )
            if not execution_phases:
                return

            # New runs stop after producing a durable approval checkpoint.
            if "init" in execution_phases:
                if await check_cancelled():
                    yield {"type": "research_cancelled", "message": "研究已取消"}
                    return
                yield {
                    "type": "phase",
                    "phase": "planning",
                    "content": "开始规划研究...",
                }
                state["phase"] = ResearchPhase.INIT.value
                async for msg in run_agent_with_streaming(self.architect):
                    yield msg
                state["messages"] = []
                if state["phase"] != ResearchPhase.AWAITING_OUTLINE_APPROVAL.value:
                    await save_checkpoint_async(
                        {"type": "planning", "status": "failed"},
                        status="failed",
                    )
                    return
                cp_event = await save_checkpoint_async(
                    {
                        "type": "outline_approval",
                        "status": "paused",
                        "stats": {"sections": len(state.get("outline", []))},
                    },
                    status="paused",
                )
                if not cp_event:
                    yield {
                        "type": "error",
                        "content": "研究大纲保存失败，请重新发起深度研究。",
                    }
                    return
                yield cp_event
                yield {
                    "type": "outline_pending_approval",
                    "session_id": session_id,
                    "outline_revision": state.get("outline_revision"),
                    "sections": state.get("outline", []),
                    "research_questions": state.get("research_questions", []),
                }
                return

            if "planning" in execution_phases:
                if await check_cancelled():
                    yield {"type": "research_cancelled", "message": "研究已取消"}
                    return
                yield {
                    "type": "phase",
                    "phase": "keyword_generation",
                    "content": "生成搜索关键词与研究假设...",
                }
                state["phase"] = ResearchPhase.PLANNING.value
                async for msg in run_agent_with_streaming(self.architect):
                    yield msg
                state["messages"] = []
                if state["phase"] == ResearchPhase.PLANNING.value:
                    cp_event = await save_checkpoint_async(
                        {"type": "planning", "status": "failed"},
                        status="failed",
                    )
                    if cp_event:
                        yield cp_event
                    return
                cp_event = await save_checkpoint_async({
                    "type": "planning",
                    "status": "completed",
                    "stats": {"sections": len(state.get("outline", []))},
                })
                if cp_event:
                    yield cp_event

            if "researching" in execution_phases:
                if await check_cancelled():
                    yield {"type": "research_cancelled", "message": "研究已取消"}
                    return
                yield {"type": "phase", "phase": "researching", "content": "开始深度搜索..."}
                state["phase"] = ResearchPhase.RESEARCHING.value
                async for msg in run_agent_with_streaming(self.scout):
                    yield msg
                state["messages"] = []
                state["phase"] = ResearchPhase.ANALYZING.value
                cp_event = await save_checkpoint_async({
                    "type": "researching",
                    "status": "completed",
                    "stats": {
                        "facts": len(state.get("facts", [])),
                        "sources": len(state.get("references", [])),
                    },
                })
                if cp_event:
                    yield cp_event

            if "analyzing" in execution_phases:
                if await check_cancelled():
                    yield {"type": "research_cancelled", "message": "研究已取消"}
                    return
                yield {"type": "phase", "phase": "analyzing", "content": "开始数据分析..."}
                state["phase"] = ResearchPhase.ANALYZING.value
                selected_analysis_tools = await self._route_analysis(state)
                if ToolName.DATA_ANALYSIS in selected_analysis_tools:
                    async for msg in run_agent_with_streaming(self.data_analyst):
                        yield msg
                    state["messages"] = []
                if ToolName.CHART_GENERATION in selected_analysis_tools:
                    async for msg in run_agent_with_streaming(self.wizard):
                        yield msg
                    state["messages"] = []
                state["phase"] = ResearchPhase.WRITING.value
                cp_event = await save_checkpoint_async({
                    "type": "analyzing",
                    "status": "completed",
                    "stats": {"charts": len(state.get("charts", []))},
                })
                if cp_event:
                    yield cp_event

            if "writing" in execution_phases:
                if await check_cancelled():
                    yield {"type": "research_cancelled", "message": "研究已取消"}
                    return
                yield {"type": "phase", "phase": "writing", "content": "开始撰写报告..."}
                state["phase"] = ResearchPhase.WRITING.value
                async for msg in run_agent_with_streaming(self.writer):
                    yield msg
                state["messages"] = []
                state["phase"] = ResearchPhase.REVIEWING.value
                cp_event = await save_checkpoint_async({
                    "type": "writing",
                    "status": "completed",
                    "stats": {"report_length": len(state.get("final_report", ""))},
                })
                if cp_event:
                    yield cp_event

            if "re_researching" in execution_phases:
                state["phase"] = ResearchPhase.RE_RESEARCHING.value
                async for msg in run_agent_with_streaming(self.scout):
                    yield msg
                state["messages"] = []
                state["phase"] = ResearchPhase.WRITING.value
                async for msg in run_agent_with_streaming(self.writer):
                    yield msg
                state["messages"] = []
                state["phase"] = ResearchPhase.REVIEWING.value

            if "revising" in execution_phases:
                state["phase"] = ResearchPhase.REVISING.value
                async for msg in run_agent_with_streaming(self.writer):
                    yield msg
                state["messages"] = []
                state["phase"] = ResearchPhase.REVIEWING.value

            # Phase 5 & 6: Review & Revise/Re-Research Loop
            while state["iteration"] < state["max_iterations"]:
                if await check_cancelled():
                    yield {"type": "research_cancelled", "message": "研究已取消"}
                    return
                yield {"type": "phase", "phase": "reviewing", "content": f"审核中（第 {state['iteration'] + 1} 轮）..."}
                state["phase"] = ResearchPhase.REVIEWING.value
                async for msg in run_agent_with_streaming(self.critic):
                    yield msg
                state["messages"] = []

                if state["phase"] == ResearchPhase.COMPLETED.value:
                    break

                if state["phase"] == ResearchPhase.RE_RESEARCHING.value:
                    if await check_cancelled():
                        yield {"type": "research_cancelled", "message": "研究已取消"}
                        return
                    yield {"type": "phase", "phase": "re_researching", "content": "根据审核反馈补充搜索..."}
                    async for msg in run_agent_with_streaming(self.scout):
                        yield msg
                    state["messages"] = []

                    yield {"type": "phase", "phase": "rewriting", "content": "基于新信息重新撰写..."}
                    state["phase"] = ResearchPhase.WRITING.value
                    async for msg in run_agent_with_streaming(self.writer):
                        yield msg
                    state["messages"] = []

                elif state["phase"] == ResearchPhase.REVISING.value:
                    if await check_cancelled():
                        yield {"type": "research_cancelled", "message": "研究已取消"}
                        return
                    yield {"type": "phase", "phase": "revising", "content": "根据反馈修订报告..."}
                    async for msg in run_agent_with_streaming(self.writer):
                        yield msg
                    state["messages"] = []
                else:
                    break

            # 只有通过确定性 Critic 门禁的报告才能进入正常完成路径。达到轮次上限、
            # Critic 输出无效或循环意外退出都必须 fail-closed，并保存可恢复的失败快照。
            if (
                state.get("phase") != ResearchPhase.COMPLETED.value
                or not state.get("quality_gate_passed", False)
            ):
                state["phase"] = ResearchPhase.QUALITY_GATE_FAILED.value
                reasons = state.get("quality_gate_reasons") or [
                    "quality_gate_not_passed"
                ]
                failed_checkpoint_event = await save_checkpoint_async(
                    {
                        "type": "quality_gate",
                        "status": "failed",
                        "stats": {
                            "quality_score": state.get("quality_score", 0.0),
                            "review_rounds": state.get("iteration", 0),
                        },
                    },
                    status="failed",
                )
                if failed_checkpoint_event:
                    yield failed_checkpoint_event
                yield {
                    "type": "quality_gate_failed",
                    "phase": ResearchPhase.QUALITY_GATE_FAILED.value,
                    "quality_score": state.get("quality_score", 0.0),
                    "review_rounds": state.get("iteration", 0),
                    "reasons": reasons,
                    "content": "报告在最大审核轮次内未通过发布质量门禁。",
                }
                return

            # 完成
            logger.info("[Graph] ========== 研究完成 ==========")
            logger.info(f"[Graph] 最终统计: facts={len(state.get('facts', []))}, charts={len(state.get('charts', []))}, iterations={state.get('iteration', 0)}")
            logger.info(f"[Graph] 报告长度: {len(state.get('final_report', ''))}")

            # 打印每个图表的详情
            for i, chart in enumerate(state.get('charts', [])):
                logger.info(f"[Graph] 图表 {i+1}: id={chart.get('id')}, title={chart.get('title')}, has_echarts={bool(chart.get('echarts_option'))}, has_image={bool(chart.get('image_base64'))}")

            # 在通知前端完成前，原子保存最终修订稿和完整 UI 状态。
            state["phase"] = ResearchPhase.COMPLETED.value
            final_checkpoint_event = await save_checkpoint_async(status="completed")
            if not final_checkpoint_event:
                yield {
                    "type": "error",
                    "content": "最终研究报告保存失败，请稍后重试。",
                }
                return
            yield final_checkpoint_event

            if user_id and self.memory_service is not None:
                try:
                    context = current_context()
                    run_id = (context.run_id if context else None) or session_id
                    async with asyncio.timeout(
                        float(os.getenv("MEMORY_TIMEOUT_SECONDS", "10"))
                    ):
                        memory_result = await self.memory_service.capture_completed_run(
                            user_id=user_id,
                            state=state,
                            run_id=run_id,
                        )
                    record_research_event(
                        "memory.persisted",
                        phase=state.get("phase"),
                        payload=memory_result,
                    )
                except Exception as exc:
                    logger.warning("Research-memory persistence failed: %s", exc)
                    record_research_event(
                        "memory.persistence_failed",
                        phase=state.get("phase"),
                        payload={"error_type": type(exc).__name__},
                    )

            # 构建前端友好的 references
            final_facts = state.get("facts", [])
            final_raw_refs = state.get("references", [])
            final_ui_refs = []
            for idx, ref in enumerate(final_raw_refs):
                fact = next((f for f in final_facts if f.get("source_url") == ref.get("url")), None)
                title = ref.get("source") or ref.get("marker") or ""
                if not title and fact:
                    content = fact.get("content", "")
                    title = content[:50] + "..." if len(content) > 50 else content
                if not title:
                    title = f"来源 {idx + 1}"
                final_ui_refs.append({
                    "id": ref.get("id", idx + 1),
                    "title": title,
                    "link": ref.get("url", ""),
                    "content": fact.get("content", "")[:200] if fact else "",
                    "source": "web"
                })

            yield {
                "type": "research_complete",
                "final_report": state.get("final_report", ""),
                "quality_score": state.get("quality_score", 0.0),
                "quality_gate_passed": state.get("quality_gate_passed", False),
                "facts_count": len(state.get("facts", [])),
                "charts_count": len(state.get("charts", [])),
                "iterations": state.get("iteration", 0),
                "references": final_ui_refs
            }

        except Exception as e:
            logger.error(f"Simplified execution error: {e}")
            # 更新检查点状态为失败
            if self.checkpoint_service and session_id:
                self.checkpoint_service.update_status(
                    session_id,
                    "failed",
                    str(e),
                    user_id=user_id,
                )
            yield {"type": "error", "content": str(e)}
        finally:
            # 先取消在飞的 agent 任务，再摘掉队列（T61 / P-16）。取消之后脱管任务
            # 不会再推事件。注意这两条语句之间没有 await，事件循环无从插入，
            # 因此**不存在**「先摘队列导致窗口内仍推事件」的竞态 —— 顺序在这里
            # 只关乎意图（先停生产者、再拆通道），不是用来堵一个真实的窗口。
            for pending in list(active_agent_tasks):
                if not pending.done():
                    pending.cancel()
            # 清理队列
            state["_message_queue"] = None

    async def run_sync(self, query: str, session_id: str) -> ResearchState:
        """
        同步执行（返回最终状态）

        用于不需要流式输出的场景
        """
        state = create_initial_state(query, session_id)
        state["max_iterations"] = self.max_iterations

        # 依次执行各阶段
        state = await self.architect.process(state)
        state = await self.scout.process(state)
        state = await self.data_analyst.process(state)
        state = await self.wizard.process(state)
        state = await self.writer.process(state)

        # 审核修订循环（支持智能路由）
        while state["iteration"] < state["max_iterations"]:
            state = await self.critic.process(state)

            if state["phase"] == ResearchPhase.COMPLETED.value:
                break

            # 智能路由：需要补充搜索
            if state["phase"] == ResearchPhase.RE_RESEARCHING.value:
                state = await self.scout.process(state)
                state["phase"] = ResearchPhase.WRITING.value
                state = await self.writer.process(state)

            # 仅需要文字修订
            elif state["phase"] == ResearchPhase.REVISING.value:
                state = await self.writer.process(state)
            else:
                break

        if (
            state.get("phase") == ResearchPhase.COMPLETED.value
            and not state.get("quality_gate_passed", False)
        ):
            state["phase"] = ResearchPhase.QUALITY_GATE_FAILED.value
            state["quality_gate_reasons"] = state.get("quality_gate_reasons") or [
                "quality_gate_not_passed"
            ]

        return state


def create_research_graph(
    llm_api_key: str = None,
    llm_base_url: str = None,
    search_api_key: str = None,
    model: str = None
) -> DeepResearchGraph:
    """
    工厂函数：创建 DeepResearch 工作流图

    所有参数都是可选的，会从配置文件读取默认值

    Args:
        llm_api_key: LLM API 密钥（可选，默认从配置读取）
        llm_base_url: LLM API 基础 URL（可选，默认从配置读取）
        search_api_key: 搜索 API 密钥（可选，默认从配置读取）
        model: 默认模型名称（可选，默认从配置读取）

    Returns:
        DeepResearchGraph 实例
    """
    return DeepResearchGraph(
        llm_api_key=llm_api_key,
        llm_base_url=llm_base_url,
        search_api_key=search_api_key,
        model=model
    )
