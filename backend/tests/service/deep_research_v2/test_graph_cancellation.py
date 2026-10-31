"""T68 验收：取消判定是**显式注入的协作者**，且注入「已取消」真的会让流水线在下一个检查点停下。

迁出前 `graph.py` 只能反向 `from router.research_router import ...` 才能拿到取消函数，
再用 `except ImportError` 把取消**静默降级为「永不取消」**（fail-open），而这个兜底
同时掩盖了两模块之间的循环依赖。更关键的是：当时**没有任何接缝**能让测试注入「已取消」
这一情形 —— 也就是说「取消真的生效」此前**不可判定**，全仓没有任何用例证明过它会
让流水线停下。本文件把「取消真的生效」与「未取消则继续」两条钉死。
"""

import pytest

import service.deep_research_v2.agents as agents_package

# 兜底：与同目录其它用例一致 —— 精简环境下 `agents` 包缺真实类时补占位名，
# 保证 graph 模块可导入（本机真实类存在，正常环境下不产生副作用）。
for _agent_name in (
    "ChiefArchitect",
    "DeepScout",
    "CodeWizard",
    "CriticMaster",
    "LeadWriter",
    "DataAnalyst",
):
    if not hasattr(agents_package, _agent_name):
        setattr(agents_package, _agent_name, type(_agent_name, (), {}))

from service.deep_research_v2.graph import DeepResearchGraph


class _Cancellation:
    """取消判定替身：可指定判定结果，并记录被查询 / 被清标志的会话。"""

    def __init__(self, cancelled: bool):
        self.cancelled = cancelled
        self.queried = []
        self.cleared = []

    def is_cancelled(self, session_id: str) -> bool:
        self.queried.append(session_id)
        return self.cancelled

    def clear_cancel_flag(self, session_id: str) -> None:
        self.cleared.append(session_id)


class _RecordingAgent:
    """最小 agent 替身：被调用时置位，并把一条自有事件投进消息队列。"""

    def __init__(self, name: str):
        self.name = name
        self.role = name
        self.started = False

    async def process(self, state):
        self.started = True
        await state["_message_queue"].put({"type": "agent_marker", "agent": self.name})
        return state


class _CheckpointSink:
    """检查点替身：只要被调用就报告成功，让流水线能走到完成。"""

    def save_checkpoint(self, **kwargs):
        return "checkpoint-1"

    def update_status(self, *args, **kwargs):
        return True


_ROLES = ("architect", "scout", "data_analyst", "wizard", "critic", "writer")


def _graph(cancellation):
    agent = _RecordingAgent("shared")
    graph = DeepResearchGraph(
        agents={role: agent for role in _ROLES},
        checkpoint_service=_CheckpointSink(),
        cancellation=cancellation,
    )
    return graph, agent


def _state():
    return {
        "query": "产业研究",
        "session_id": "session-1",
        "phase": "researching",
        "iteration": 0,
        "max_iterations": 0,
        "messages": [],
        "facts": [],
        "references": [],
        "charts": [],
        "knowledge_graph": {"nodes": [], "edges": []},
        "final_report": "",
    }


@pytest.mark.asyncio
async def test_injected_cancelled_stops_pipeline_at_checkpoint():
    """注入「已取消」→ 在下一个检查点停下，且不再启动任何 agent。"""
    cancellation = _Cancellation(cancelled=True)
    graph, agent = _graph(cancellation)

    events = [event async for event in graph._run_state_machine(_state())]

    assert any(event.get("type") == "research_cancelled" for event in events), (
        f"注入「已取消」后应报取消事件，实际事件类型：{[e.get('type') for e in events]}"
    )
    assert agent.started is False, "已取消就不得再启动 agent"
    assert cancellation.queried == ["session-1"], (
        "判定必须来自注入的协作者、且在**下一个**检查点就被问到（只问一次即停）"
    )
    assert cancellation.cleared == ["session-1"], (
        "研究开始时清取消标志这一步也必须走注入的协作者"
    )


@pytest.mark.asyncio
async def test_injected_not_cancelled_lets_pipeline_continue():
    """注入「未取消」→ 流水线继续（agent 真的被执行），且不报取消。"""
    cancellation = _Cancellation(cancelled=False)
    graph, agent = _graph(cancellation)

    events = [event async for event in graph._run_state_machine(_state())]

    assert agent.started is True, "未取消时应继续执行 agent"
    assert not any(event.get("type") == "research_cancelled" for event in events)


def test_default_collaborator_is_the_neutral_cancel_module():
    """生产路径（不注入）默认走中立模块 core/research_cancel，而不是 router。"""
    from core import research_cancel

    graph = DeepResearchGraph(
        agents={role: _RecordingAgent(role) for role in _ROLES},
        checkpoint_service=None,
    )
    assert graph._cancellation is research_cancel
