import pytest

import service.deep_research_v2.agents as agents_package


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


from service.deep_research_v2.graph import DeepResearchGraph


class FakeArchitect:
    name = "ChiefArchitect"

    async def process(self, state):
        state.update(
            {
                "phase": "awaiting_outline_approval",
                "outline_revision": "revision-1",
                "outline": [
                    {
                        "id": f"section-{index}",
                        "title": f"章节 {index}",
                        "description": f"描述 {index}",
                        "section_type": "mixed",
                        "requires_data": False,
                        "requires_chart": False,
                    }
                    for index in range(1, 4)
                ],
                "research_questions": [
                    {"id": f"question-{index}", "text": f"问题 {index}"}
                    for index in range(1, 4)
                ],
            }
        )
        return state


class FailingCheckpointService:
    def save_checkpoint(self, **_kwargs):
        return None


class CompletingCritic:
    name = "CriticMaster"

    async def process(self, state):
        state["phase"] = "completed"
        state["quality_gate_passed"] = True
        state["final_report"] = "final revised report"
        return state


class BypassingCritic:
    name = "CriticMaster"

    async def process(self, state):
        # 模拟 Agent 误把 phase 写成 completed；状态机仍必须由门禁标志拦截。
        state["phase"] = "completed"
        state["quality_gate_passed"] = False
        state["quality_gate_reasons"] = ["blocking_issues_present"]
        state["final_report"] = "unsafe report"
        return state


class RecordingCheckpointService:
    def __init__(self):
        self.saved = []
        self.status_updates = []

    def save_checkpoint(self, **kwargs):
        self.saved.append(kwargs)
        return "checkpoint-1"

    def update_status(self, *args, **kwargs):
        self.status_updates.append((args, kwargs))
        return True


_AGENT_ROLES = ("architect", "scout", "data_analyst", "wizard", "critic", "writer")


class _NoCancellation:
    """取消判定替身（T68）：恒「未取消」，把用例与 Redis 解耦。

    迁出前这几条用例是 monkeypatch `graph_module` 上的取消函数；T68 之后取消判定
    是构造器里**显式注入的协作者**，所以改为从这里注入 —— 走与生产代码同一个接缝。
    """

    def is_cancelled(self, session_id):
        return False

    def clear_cancel_flag(self, session_id):
        pass


def _graph(agent, checkpoint_service):
    """走真实构造器：六个角色全部注入同一个替身（该阶段用不到的不会被访问）。"""
    return DeepResearchGraph(
        agents={role: agent for role in _AGENT_ROLES},
        checkpoint_service=checkpoint_service,
        cancellation=_NoCancellation(),
    )


@pytest.mark.asyncio
async def test_failed_checkpoint_never_emits_outline_approval():
    graph = _graph(FakeArchitect(), FailingCheckpointService())
    state = {
        "query": "产业研究",
        "session_id": "session-1",
        "phase": "init",
        "messages": [],
        "facts": [],
        "references": [],
        "charts": [],
        "knowledge_graph": {"nodes": [], "edges": []},
        "final_report": "",
    }

    events = [event async for event in graph._run_state_machine(state)]

    assert any(event["type"] == "error" for event in events)
    assert not any(
        event["type"] == "outline_pending_approval" for event in events
    )


@pytest.mark.asyncio
async def test_completion_saves_final_state_and_ui_before_emitting_report():
    graph = _graph(CompletingCritic(), RecordingCheckpointService())
    state = {
        "query": "industry research",
        "session_id": "session-1",
        "phase": "reviewing",
        "iteration": 0,
        "max_iterations": 1,
        "messages": [],
        "facts": [],
        "references": [],
        "charts": [],
        "knowledge_graph": {"nodes": [], "edges": []},
        "final_report": "pre-review draft",
    }

    events = [event async for event in graph._run_state_machine(state)]

    assert graph.checkpoint_service.saved
    final_save = graph.checkpoint_service.saved[-1]
    assert final_save["status"] == "completed"
    assert final_save["state"]["phase"] == "completed"
    assert final_save["final_report"] == "final revised report"
    assert final_save["ui_state"]["streaming_report"] == "final revised report"
    event_types = [event.get("type") for event in events]
    assert event_types.index("checkpoint_saved") < event_types.index("research_complete")
    assert any(
        event.get("type") == "research_complete"
        and event.get("final_report") == "final revised report"
        for event in events
    )


@pytest.mark.asyncio
async def test_failed_final_checkpoint_never_emits_research_complete():
    graph = _graph(CompletingCritic(), FailingCheckpointService())
    state = {
        "query": "industry research",
        "session_id": "session-1",
        "phase": "reviewing",
        "iteration": 0,
        "max_iterations": 1,
        "messages": [],
        "facts": [],
        "references": [],
        "charts": [],
        "knowledge_graph": {"nodes": [], "edges": []},
        "final_report": "pre-review draft",
    }

    events = [event async for event in graph._run_state_machine(state)]

    assert any(event.get("type") == "error" for event in events)
    assert not any(event.get("type") == "research_complete" for event in events)


@pytest.mark.asyncio
async def test_completed_phase_cannot_bypass_quality_gate():
    checkpoint = RecordingCheckpointService()
    graph = _graph(BypassingCritic(), checkpoint)
    state = {
        "query": "industry research",
        "session_id": "session-1",
        "phase": "reviewing",
        "iteration": 0,
        "max_iterations": 1,
        "messages": [],
        "facts": [],
        "references": [],
        "charts": [],
        "knowledge_graph": {"nodes": [], "edges": []},
        "final_report": "pre-review draft",
    }

    events = [event async for event in graph._run_state_machine(state)]

    assert checkpoint.saved[-1]["status"] == "failed"
    assert checkpoint.saved[-1]["state"]["phase"] == "quality_gate_failed"
    assert any(event.get("type") == "quality_gate_failed" for event in events)
    assert not any(event.get("type") == "research_complete" for event in events)
