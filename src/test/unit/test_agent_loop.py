from __future__ import annotations

from datetime import timedelta

import pytest

from src.agent.schema.model_config import FrozenModelBinding, ResolvedModelRevision
from src.agent.schema.runtime import ContextPacket, ExecutionContext
from src.agent.llm.runtime import FakeModelRuntime
from src.agent.backends.persistence.models import utcnow
from src.agent.runtime.agent_loop import AgentCancelled, AgentLoop, AgentLoopExhausted
from src.agent.runtime.tool_runtime import ToolPolicy, ToolRegistry, ToolResult


class Token:
    def __init__(self, cancelled: bool = False) -> None:
        self.cancelled = cancelled

    def is_cancelled(self) -> bool:
        return self.cancelled


class FakeTool:
    name = "test_tool"

    def __init__(self) -> None:
        self.calls = 0

    def invoke(self, arguments, context):
        self.calls += 1
        return ToolResult(ok=True, content={"echo": arguments, "task_id": context.task_id})


def binding():
    return FrozenModelBinding(
        binding_key="tool",
        purpose="chat",
        primary=ResolvedModelRevision(model_config_id="x", version=1),
    )


def execution(cancelled: bool = False):
    return ExecutionContext(
        tenant_id="t",
        user_id="u",
        task_id="task",
        thread_id="thread",
        lease_token="lease",
        cancellation_token=Token(cancelled),
        deadline=utcnow() + timedelta(minutes=1),
    )


def packet():
    return ContextPacket(
        role="deep_scout", task_id="task", query="q", deadline=utcnow() + timedelta(minutes=1)
    )


def test_agent_loop_observes_tool_result_and_stops() -> None:
    tool = FakeTool()
    runtime = FakeModelRuntime(
        {
            "tool": [
                {"action": "tool", "tool_name": "test_tool", "arguments": {"x": 1}},
                {"action": "final", "answer": "done"},
            ]
        }
    )
    answer = AgentLoop(ToolRegistry([tool]), max_steps=2).run(
        agent_name="deep_scout",
        model=runtime.build_chat_model("t", binding()),
        packet=packet(),
        execution=execution(),
        policy=ToolPolicy(frozenset({"test_tool"})),
        prompt="prompt",
    )
    assert answer == "done"
    assert tool.calls == 1
    assert "test_tool" in runtime.calls["tool"][1]


def test_agent_loop_enforces_max_steps_and_cancellation() -> None:
    runtime = FakeModelRuntime(
        {"tool": [{"action": "tool", "tool_name": "missing", "arguments": {}}]}
    )
    with pytest.raises(AgentLoopExhausted):
        AgentLoop(ToolRegistry(), max_steps=1).run(
            agent_name="deep_scout",
            model=runtime.build_chat_model("t", binding()),
            packet=packet(),
            execution=execution(),
            policy=ToolPolicy(frozenset()),
            prompt="prompt",
        )
    with pytest.raises(AgentCancelled):
        AgentLoop(ToolRegistry(), max_steps=1).run(
            agent_name="deep_scout",
            model=runtime.build_chat_model("t", binding()),
            packet=packet(),
            execution=execution(cancelled=True),
            policy=ToolPolicy(frozenset()),
            prompt="prompt",
        )

