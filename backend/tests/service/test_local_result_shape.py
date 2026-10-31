"""T45 回归测试：本地知识库结果形状统一（方案 B）。

背景：本地检索结果的形状此前有**三处**独立实现且字段已分叉 ——
`deep_research_v2/agents/scout.py`（V2 主链路）用 `title` / `site_name` / `is_local`，
而 `dr_g.py` 与 `tool_executor.py`（V1 备选路线）用 `name` / `siteName` / `source`，
连 `url` 前缀都不同。字段名不一致的代价是**取错名字不会报错，只会静默拿到空标题 /
「未知来源」**。现统一以 scout.py 的字段为准，三处共用
`retrieval_service.format_local_search_results`。

验证分三层，**都不依赖基础设施**（`retrieval_service` 的导入不连 Milvus，只加载模块）：

1. **行为层** —— 直接断言规范形状的字段集合、url 前缀、截断口径与缺字段降级；
2. **AST 层** —— 三个模块都调用共享 formatter，且都不再自行拼 `local://`；
3. **契约层** —— dr_g 的 `memory` 输出键名仍是 `name` / `siteName` / `siteIcon`，
   即本轮**没有**改动 SSE `search_result_item` 与前端 `Source` 组件的既有契约。
"""

import ast
import pathlib

import pytest

from service.retrieval_service import format_local_search_results

SERVICE_DIR = pathlib.Path(__file__).resolve().parents[2] / "app" / "service"

SCOUT = SERVICE_DIR / "deep_research_v2" / "agents" / "scout.py"
DR_G = SERVICE_DIR / "dr_g.py"
TOOL_EXECUTOR = SERVICE_DIR / "tool_executor.py"

CALLERS = [SCOUT, DR_G, TOOL_EXECUTOR]

# 规范形状的**完整**字段集合（以 scout.py 为准）。任何增减都必须同步改这里。
CANONICAL_FIELDS = {
    "url", "title", "summary", "snippet", "site_name",
    "date", "score", "is_local", "kb_name", "doc_id",
}

RAW_ROW = {
    "document_id": "doc-1",
    "document_name": "安全手册.pdf",
    "content_with_weight": "正" * 800,
    "score": 0.87,
}


def _called_names(path: pathlib.Path) -> set:
    """AST 取出模块内被**调用**的裸函数名（不导入模块，故无须依赖）。"""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            names.add(node.func.id)
    return names


# --------------------------------------------------------------------------
# 1) 行为层
# --------------------------------------------------------------------------

def test_canonical_field_set_is_locked():
    """🔒 形状断言：字段集合必须与规范完全一致（多一个、少一个都失败）。"""
    row = format_local_search_results([RAW_ROW], "kb-x")[0]

    assert set(row) == CANONICAL_FIELDS


def test_url_prefix_is_the_shared_one():
    """url 前缀统一为 `local://kb/<kb_name>/<doc_id>`（此前 dr_g 少了 `/kb`）。"""
    row = format_local_search_results([RAW_ROW], "kb-x")[0]

    assert row["url"] == "local://kb/kb-x/doc-1"


def test_scout_field_names_are_used():
    """统一后的字段名取自 scout.py：`title` / `site_name` / `is_local`。"""
    row = format_local_search_results([RAW_ROW], "kb-x")[0]

    assert row["title"] == "安全手册.pdf"
    assert row["site_name"] == "本地知识库"
    assert row["is_local"] is True
    assert row["kb_name"] == "kb-x"
    assert row["doc_id"] == "doc-1"
    # 旧字段名不得再出现（否则消费方又会踩「取错名字静默拿空值」）
    assert "name" not in row
    assert "siteName" not in row
    assert "source" not in row


def test_summary_and_snippet_truncation():
    """截断口径沿用 scout.py：summary ≤ 500、snippet ≤ 200。"""
    row = format_local_search_results([RAW_ROW], "kb-x")[0]

    assert len(row["summary"]) == 500
    assert len(row["snippet"]) == 200


def test_missing_fields_degrade_without_raising():
    """原始结果缺字段时给出占位值，而不是抛异常（检索侧字段并不总是齐全）。"""
    row = format_local_search_results([{}], "kb-x")[0]

    assert row["url"] == "local://kb/kb-x/unknown"
    assert row["title"] == "N/A"
    assert row["summary"] == ""
    assert row["score"] == 0
    assert row["doc_id"] is None


def test_empty_input_returns_empty_list():
    assert format_local_search_results([], "kb-x") == []


# --------------------------------------------------------------------------
# 2) AST 层：三处确实并轨
# --------------------------------------------------------------------------

@pytest.mark.parametrize("path", CALLERS)
def test_module_calls_the_shared_formatter(path):
    assert "format_local_search_results" in _called_names(path), (
        f"{path.name} 未调用共享形状函数，本地结果形状可能又分叉"
    )


@pytest.mark.parametrize("path", CALLERS)
def test_module_no_longer_builds_local_urls(path):
    """🔒 并轨证据：`local://` 只应出现在 retrieval_service 里。"""
    assert "local://" not in path.read_text(encoding="utf-8"), (
        f"{path.name} 仍自行拼本地 URL，未使用共享形状函数"
    )


# --------------------------------------------------------------------------
# 3) 契约层：SSE / 前端消费契约未被改动
# --------------------------------------------------------------------------

def test_dr_g_memory_output_keys_are_unchanged():
    """dr_g 归一化后的键名必须仍是 `name` / `siteName` / `siteIcon`。

    这三者经 SSE `search_result_item` 直达前端 `Source` 组件
    （`chat/index.tsx` 渲染 `search_results`，`source.tsx` 读 `name` / `siteIcon`）。
    本票只统一**输入**形状，不动这条**输出**契约 —— 因此前端无需改动。
    """
    source = DR_G.read_text(encoding="utf-8")

    assert '"name":' in source
    assert '"siteName":' in source
    assert '"siteIcon":' in source


def test_dr_g_accepts_both_field_spellings():
    """归一化处必须同时兼容统一后的本地字段与网络结果的原有字段。"""
    source = DR_G.read_text(encoding="utf-8")

    assert "result.get('title'" in source, "未兼容统一后的本地字段 title"
    assert "result.get('site_name'" in source, "未兼容统一后的本地字段 site_name"
    assert "result.get('siteName'" in source, "网络结果的 siteName 读取被破坏"
