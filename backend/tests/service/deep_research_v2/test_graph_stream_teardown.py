"""T61 / P-16：SSE 流在研究中途被关闭时，在飞的 agent 任务必须被取消。

复现依据：`.runlogs/t49_backend8001c.log`

  - `10:25:03.786` 在 `service.py` 的 SSE 产出点被抛入（当时写作 `yield self._format_sse(event)`；
    T67 之后该处为 `yield sse_frame(event)` —— 符号名改了，位置语义不变）
    `GeneratorExit` —— Starlette 检测到 SSE 客户端断连后关闭了响应生成器；
  - 此后 DeepScout 的 `asyncio.create_task(execute_agent())` 任务**脱管继续运行**
    （`e9e2b878` 续跑 6m42s：`10:25:04` → `10:31:45`），期间每条事件都因
    `_run_state_machine` 的 finally 已把 `state["_message_queue"]` 置为 None 而落到
    `[SSE] No queue available`（该会话 95 条；全文合计 246 条 = 本会话 95 + `50c3dde7`
    151，后者是**同一机制**但断连未留下 contextvar 异常记录），`search_results` 丢得最集中
    （全文 120 = 43 + 77）。

本用例不依赖任何基础设施（不起 Postgres/Redis/Milvus，也不调 LLM）：用一个替身 agent
驱动 `_run_state_machine`，在流式阶段 `aclose()` 模拟断连，断言替身 agent 收到取消，
且**真实 `BaseAgent.add_message`** 不再产生「无队列」告警。
"""

import asyncio
import logging

import pytest

import service.deep_research_v2.agents as agents_package


# 兜底：真实 agent 类缺失时（精简环境）给 `agents` 包占位名，保证 graph 模块可导入。
# 本机真实类存在，因此这一段在正常环境下不产生任何副作用。
for agent_name in (
    "ChiefArchitect",
    "DeepScout",
    "CodeWizard",
    "CriticMaster",
    "LeadWriter",
    "DataAnalyst",
):
    if not hasattr(agents_package, agent_name):
        setattr(agents_package, agent_name, type(agent_name, (), {}))


from service.deep_research_v2.agents.base import BaseAgent
from service.deep_research_v2.graph import DeepResearchGraph


class NoQueueWarningRecorder(logging.Handler):
    """只收集 `[SSE] No queue available` 告警 —— 即 P-16 的原始症状。"""

    def __init__(self):
        super().__init__(level=logging.WARNING)
        self.messages = []

    def emit(self, record):
        message = record.getMessage()
        if "No queue available" in message:
            self.messages.append(message)


class StreamingScout:
    """替身 agent：复用**真实**的 `BaseAgent.add_message` 向实时队列推事件。"""

    name = "DeepScout"
    role = "scout"
    logger = logging.getLogger("Agent.DeepScout")
    # 绑定基类的真实实现，避免在测试里重实现被测逻辑（否则断言可能恒真）。
    add_message = BaseAgent.add_message

    def __init__(self):
        self.cancelled = False
        self.completed = False

    async def process(self, state):
        try:
            for index in range(2000):
                await asyncio.sleep(0.002)
                self.add_message(state, "search_results", index)
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        self.completed = True
        return state


def _researching_state():
    return {
        "query": "产业研究",
        "session_id": "session-1",
        "phase": "researching",
        "iteration": 0,
        "max_iterations": 1,
        "messages": [],
        "facts": [],
        "references": [],
        "charts": [],
        "knowledge_graph": {"nodes": [], "edges": []},
        "final_report": "",
    }


class _NoCancellation:
    """取消判定替身（T68）：恒「未取消」，把用例与 Redis 解耦。

    迁出前本用例 monkeypatch `graph_module` 上的取消函数；T68 之后取消判定是构造器
    里**显式注入的协作者**，故改为从这里注入 —— 走与生产代码同一个接缝。
    """

    def is_cancelled(self, session_id):
        return False

    def clear_cancel_flag(self, session_id):
        pass


def _graph_with_scout(scout):
    # 走真实构造器：六个角色全部注入同一个替身（与原 object.__new__ 写法等价）
    graph = DeepResearchGraph(
        agents={
            attribute: scout
            for attribute in (
                "architect",
                "scout",
                "data_analyst",
                "wizard",
                "critic",
                "writer",
            )
        },
        checkpoint_service=None,
        cancellation=_NoCancellation(),
    )
    return graph


@pytest.mark.asyncio
async def test_closing_stream_cancels_inflight_agent():
    scout = StreamingScout()
    graph = _graph_with_scout(scout)
    state = _researching_state()

    recorder = NoQueueWarningRecorder()
    scout.logger.addHandler(recorder)
    try:
        stream = graph._run_state_machine(state)
        received = 0
        async for event in stream:
            if event.get("type") == "search_results":
                received += 1
                if received >= 2:
                    break
        assert received >= 2, "预期在 agent 流式阶段收到事件"

        await stream.aclose()  # 模拟 SSE 客户端断连
        # 给取消/脱管任务留出推进窗口：足够让一个**未被取消**的任务再推若干条事件
        # （替身每 2ms 推一条），从而让下面的断言具备判别力。
        await asyncio.sleep(0.05)

        cancelled = scout.cancelled
        completed = scout.completed
        dropped = list(recorder.messages)
        assert (cancelled, completed) == (True, False), (
            f"流被关闭后应取消在飞任务：cancelled={cancelled}（应 True）、"
            f"completed={completed}（应 False）"
        )
        assert dropped == [], (
            f"流关闭后不应再产生 `[SSE] No queue available`（P-16 症状），"
            f"实际 {len(dropped)} 条，前 3 条：{dropped[:3]}"
        )
    finally:
        scout.logger.removeHandler(recorder)
