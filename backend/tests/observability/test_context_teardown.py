"""跨 Context 退出上下文管理器时不得抛 ValueError（T62；SSE 断连 teardown 族）。

背景：SSE 客户端断连时 Starlette 会关闭响应生成器，`GeneratorExit` 展开经过
`service.py` 的 `with bind_context(...), bind_run_usage()`。这些上下文管理器的 token 是在
**另一个** Context 里 `set` 的，于是 `__exit__` 里的 `ContextVar.reset(token)` 抛
`ValueError: … was created in a different Context`，把干净的关闭路径掩盖成 context 错误。

归档证据 `.runlogs/t49_backend8001c.log`（两条 ERROR 同源但**分层**）：
  - `10:25:03.786` `opentelemetry.context | ERROR | Failed to detach context`
    —— **第三方 OpenTelemetry 自己的** `current_context` ContextVar 在 detach 时报的，
    属 **OTel 侧**；本票**修的不是它**，修复后重跑仍会出现（OTel 自行 catch 后只记 ERROR）。
  - `10:25:03.838` `asyncio | ERROR | Task exception was never retrieved`
    （`Token var=<ContextVar name='observability_context' …> was created in a different Context`）
    —— 本仓 `bind_context` / `bind_run_usage` 的 token 重置，**这条才是本票的靶子**。

故本文件的用例只钉**本仓三处** `reset` 的行为；OTel 侧 detach 的收口另记（见 TRACKER）。
不依赖任何基础设施。
"""

from __future__ import annotations

import asyncio
import logging
from contextvars import ContextVar, copy_context

import pytest

from observability.context import bind_context, current_context, reset_context_var
from observability.events import bind_event_recorder, bind_run_usage


def _managers():
    """每个用例用**新建**的上下文管理器，避免复用已被 enter 的实例。"""
    return [
        pytest.param(lambda: bind_context(request_id="req-teardown"), id="bind_context"),
        pytest.param(bind_run_usage, id="bind_run_usage"),
        pytest.param(
            lambda: bind_event_recorder(lambda **_kwargs: None),
            id="bind_event_recorder",
        ),
    ]


@pytest.mark.parametrize("manager_factory", _managers())
def test_exit_from_another_context_does_not_raise(manager_factory):
    manager = manager_factory()
    other = copy_context()
    other.run(manager.__enter__)  # token 在【另一个 Context】里创建

    # 在本 Context 退出：修复前抛 ValueError("… created in a different Context")
    manager.__exit__(None, None, None)


def test_exit_in_the_same_context_still_restores_the_previous_value():
    """回归：同 Context 下必须照常复原，守卫不能把正常路径也吞掉。"""

    assert current_context() is None
    with bind_context(request_id="req-normal"):
        inner = current_context()
        assert inner is not None and inner.request_id == "req-normal"
    assert current_context() is None


@pytest.mark.asyncio
async def test_async_generator_closed_from_another_task_does_not_raise():
    """复现生产机制：生成器在一个任务里推进，被另一个任务 `aclose()`。"""

    seen = []

    async def stream():
        with bind_context(request_id="req-stream"), bind_run_usage():
            seen.append(current_context().request_id)
            yield "chunk"
            seen.append("resumed-after-yield")

    stream_gen = stream()
    assert await stream_gen.__anext__() == "chunk"
    assert seen == ["req-stream"]

    async def teardown():
        # 新任务 → 复制出的另一个 Context，等价于 Starlette 的断连 teardown
        await stream_gen.aclose()

    await asyncio.create_task(teardown())

    # 生成器确已被关闭：再推一次只应得到 StopAsyncIteration。
    # （原先这里是 `assert seen == ["req-stream"]` —— Python 保证 `GeneratorExit` 之后
    # 生成器不会恢复执行，该断言**在任何实现下都成立**，属恒真断言（终审 §4 第三轮 findings #4；`43f6cd1` message 中的「小-6」为报告原始编号）。
    # 现改为断言「已关闭」这一后置条件；本用例的**真正判别力**仍来自上面 `aclose()`
    # 不抛异常 —— 三处守卫回退时它会 4 failed。此断言属文档级，不夸大其判别力。）
    with pytest.raises(StopAsyncIteration):
        await stream_gen.__anext__()


def test_reset_with_a_token_from_another_var_still_raises():
    """守卫不得吞掉「拿错 var」的编程错误（T62 §3 复核 Standards finding）。

    `ContextVar.reset` 的两类 `ValueError` 措辞不同：跨 Context 的是
    ``… created in a different Context``，拿错 var 的是 ``… created by a different
    ContextVar``。后者必须原样抛出 —— 否则守卫会把真实误用静默吃掉。
    """

    var_a = ContextVar("reset_guard_var_a")
    var_b = ContextVar("reset_guard_var_b")
    token = var_b.set("value")
    try:
        with pytest.raises(ValueError, match="different ContextVar"):
            reset_context_var(var_a, token)
    finally:
        var_b.reset(token)


def test_cross_context_reset_is_skipped_and_logged(caplog):
    """跨 Context 跳过时必须留下 debug 记录（此前该分支无任何断言）。"""

    var = ContextVar("reset_guard_cross_ctx")
    other = copy_context()
    token = other.run(lambda: var.set("value"))

    with caplog.at_level(logging.DEBUG, logger="observability.context"):
        reset_context_var(var, token)

    skipped = [
        record
        for record in caplog.records
        if getattr(record, "event", None) == "observability.context_reset_skipped"
    ]
    assert len(skipped) == 1, caplog.records
    assert skipped[0].levelno == logging.DEBUG
    assert skipped[0].details["var"] == "reset_guard_cross_ctx"
