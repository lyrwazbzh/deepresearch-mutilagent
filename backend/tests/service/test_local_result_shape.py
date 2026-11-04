"""本地知识库结果形状：与 DeepScout 规范字段对齐。

统一入口：`retrieval_service.format_local_search_results`。
"""

import ast
import pathlib

import pytest

from service.retrieval_service import format_local_search_results

SERVICE_DIR = pathlib.Path(__file__).resolve().parents[2] / "app" / "service"
SCOUT = SERVICE_DIR / "deep_research" / "agents" / "scout.py"

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
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            names.add(node.func.id)
    return names


def test_canonical_field_set_is_locked():
    row = format_local_search_results([RAW_ROW], "kb-x")[0]
    assert set(row) == CANONICAL_FIELDS


def test_url_prefix_is_the_shared_one():
    row = format_local_search_results([RAW_ROW], "kb-x")[0]
    assert row["url"] == "local://kb/kb-x/doc-1"


def test_scout_field_names_are_used():
    row = format_local_search_results([RAW_ROW], "kb-x")[0]
    assert row["title"] == "安全手册.pdf"
    assert row["site_name"] == "本地知识库"
    assert row["is_local"] is True
    assert row["kb_name"] == "kb-x"
    assert row["doc_id"] == "doc-1"
    assert "name" not in row
    assert "siteName" not in row
    assert "source" not in row


def test_summary_and_snippet_truncation():
    row = format_local_search_results([RAW_ROW], "kb-x")[0]
    assert len(row["summary"]) == 500
    assert len(row["snippet"]) == 200


def test_missing_fields_degrade_without_raising():
    row = format_local_search_results([{}], "kb-x")[0]
    assert row["url"] == "local://kb/kb-x/unknown"
    assert row["title"] == "N/A"
    assert row["summary"] == ""
    assert row["score"] == 0
    assert row["doc_id"] is None


def test_empty_input_returns_empty_list():
    assert format_local_search_results([], "kb-x") == []


def test_scout_calls_the_shared_formatter():
    assert "format_local_search_results" in _called_names(SCOUT)


def test_scout_no_longer_builds_local_urls():
    assert "local://" not in SCOUT.read_text(encoding="utf-8")
