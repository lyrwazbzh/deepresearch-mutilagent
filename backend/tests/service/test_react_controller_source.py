"""批次 7-3 审查发现 → T45 回归修复：`react_controller` 的本地结果来源标记。

背景：T45 把本地知识库结果统一为 scout.py 的规范形状（`title` / `site_name` /
`is_local`，**没有** `source` 字段）。但 V1 ReAct 路线的
`ReActContext.get_collected_data_summary()` 仍用 `item.get('source', 'unknown')`
取来源 —— 于是本地条目在改造后**从 `(local)` 静默降级为 `(unknown)`**。
字段名不一致不会报错，只会悄悄丢信息（这正是 T45 想根治的病）。

本文件把该行为锁死：无论条目是否携带 `source`，本地条目都必须渲染为 `(local)`。
两层验证，均不依赖基础设施（`react_controller` / `retrieval_service` 的导入都不连
Milvus / PG，只加载模块）：

1. **行为层** —— 各来源（web / stock_api / bidding_api / 本地 / 真正的未知）的渲染；
2. **跨边界层** —— 把 `format_local_search_results` 的**真实输出**灌进
   `collected_data`（含走真实 `add_observation` 路径），端到端断言 `(local)`。
"""

import pathlib

import pytest

from service.react_controller import Observation, ReActContext, ToolType
from service.retrieval_service import format_local_search_results

REACT = pathlib.Path(__file__).resolve().parents[2] / "app" / "service" / "react_controller.py"

# T45 规范形状的一条本地结果（**无** source 字段）。
LOCAL_ITEM = {
    "url": "local://kb/kb-x/doc-1",
    "title": "安全手册.pdf",
    "summary": "本地内容",
    "snippet": "本地内容",
    "site_name": "本地知识库",
    "date": "",
    "score": 0.9,
    "is_local": True,
    "kb_name": "kb-x",
    "doc_id": "doc-1",
}


def _summary(*items):
    ctx = ReActContext("测试问题")
    ctx.collected_data = list(items)
    return ctx.get_collected_data_summary()


# ---------- 1. 行为层 ----------

def test_local_item_without_source_renders_local():
    """T45 规范形状（无 source）的本地条目 → (local)，绝不是 (unknown)。"""
    out = _summary(LOCAL_ITEM)
    assert "(local)" in out
    assert "(unknown)" not in out
    assert "安全手册.pdf" in out


def test_web_item_still_renders_web():
    out = _summary({"name": "网页", "summary": "摘要", "source": "web"})
    assert "(web)" in out


@pytest.mark.parametrize("source", ["stock_api", "bidding_api"])
def test_other_explicit_sources_untouched(source):
    """其它显式来源（行情 / 招标）不受本次修复影响。"""
    out = _summary({"name": "行情", "summary": "摘要", "source": source})
    assert f"({source})" in out


def test_truly_unknown_item_renders_unknown():
    """既无 source 也无 is_local 的条目仍应 (unknown)，不得被误标为本地。"""
    out = _summary({"title": "裸条目", "summary": "摘要"})
    assert "(unknown)" in out
    assert "(local)" not in out


def test_empty_source_with_is_local_renders_local():
    """显式空串 source + is_local → 不能因空串被误判 unknown。"""
    out = _summary({"title": "x", "summary": "y", "source": "", "is_local": True})
    assert "(local)" in out
    assert "(unknown)" not in out


def test_explicit_source_wins_over_is_local():
    """显式 source 优先：未来本地条目若自带 source，也不得被 is_local 覆盖。"""
    out = _summary({"title": "x", "summary": "y", "source": "kb", "is_local": True})
    assert "(kb)" in out


# ---------- 2. 跨边界层 ----------

def test_real_formatter_output_flows_through_to_local():
    """把 T45 的真实产出灌进来，端到端锁死「统一形状 → 消费者」的接缝。"""
    raw = [{
        "document_id": "doc-7",
        "document_name": "行业白皮书.pdf",
        "content_with_weight": "正文" * 10,
        "score": 0.8,
    }]
    formatted = format_local_search_results(raw, "政策库")
    assert "source" not in formatted[0]  # 前提：统一形状确实没有 source 字段
    out = _summary(*formatted)
    assert "(local)" in out
    assert "(unknown)" not in out
    assert "行业白皮书.pdf" in out


def test_add_observation_appends_local_and_renders_local():
    """走真实 add_observation 路径（knowledge_search 结果进 collected_data）。"""
    ctx = ReActContext("测试问题")
    formatted = format_local_search_results(
        [{"document_id": "d1", "document_name": "文档A",
          "content_with_weight": "内容", "score": 1.0}],
        "库A",
    )
    ctx.add_observation(Observation(
        tool=ToolType.KNOWLEDGE_SEARCH.value, success=True, result=formatted
    ))
    assert ctx.collected_data  # 确实被收集进上下文
    out = ctx.get_collected_data_summary()
    assert "(local)" in out
    assert "(unknown)" not in out
