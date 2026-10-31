"""T41 回归测试：三个上传入口共用同一套安全实现，但白名单各自保留（方案 A）。

背景：T03 只把 `document_router` 接上了 `core/upload_security`，另外两个入口
（`attachment_router.py`、`knowledge_router.py`）仍把**客户端文件名**拼进路径 ——
`os.path.join(UPLOAD_DIR, f"{uuid}_{file.filename}")` 里的 `../../` 片段可以写出
`UPLOAD_DIR`（与事实 F-02 同源），且三处各自维护一份扩展名解析与白名单。

用户裁决采用**方案 A**：只统一*代码实现*、白名单各自保留 —— 预期**零行为变更**，只堵路径穿越。

由于 `attachment_router` / `knowledge_router` 会连带引入项目重依赖（实测缺 `tinytag` 即导入失败），
本文件分两层验证，**都不依赖基础设施**：

1. **纯函数层** —— 用各路由自己的白名单跑共享实现，验证穿越名被净化、合法扩展名仍被接受；
2. **源码层** —— 用 `ast` 解析路由源码，断言「不再自造扩展名解析」「落盘不再引用客户端文件名」，
   并**锁定三份白名单的成员集合**：后者正是「零行为变更」的证据。
"""

import ast
import pathlib
import re

import pytest
from fastapi import HTTPException

from core.upload_security import (
    ensure_supported_extension,
    safe_filename,
)

ROUTER_DIR = pathlib.Path(__file__).resolve().parents[2] / "app" / "router"

ATTACHMENT = ROUTER_DIR / "attachment_router.py"
KNOWLEDGE = ROUTER_DIR / "knowledge_router.py"
DOCUMENT = ROUTER_DIR / "document_router.py"

# 方案 A 的「零行为变更」基准：以下集合必须与改造前完全一致。
EXPECTED_ATTACHMENT_EXTENSIONS = {
    ".pdf", ".docx", ".doc", ".txt", ".md", ".html", ".xlsx", ".xls", ".pptx", ".ppt",
    ".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp",
    ".py", ".js", ".ts", ".json", ".yaml", ".yml", ".xml", ".csv",
}

EXPECTED_DOCUMENT_FILE_TYPES = {".pdf", ".docx", ".xlsx", ".xls", ".txt"}

# (路由源码, 白名单常量名, 期望集合)
WHITELISTS = [
    (ATTACHMENT, "ALLOWED_EXTENSIONS", EXPECTED_ATTACHMENT_EXTENSIONS),
    (KNOWLEDGE, "ALLOWED_EXTENSIONS", EXPECTED_ATTACHMENT_EXTENSIONS),
    (DOCUMENT, "SUPPORTED_FILE_TYPES", EXPECTED_DOCUMENT_FILE_TYPES),
]


def _source(path: pathlib.Path) -> str:
    return path.read_text(encoding="utf-8")


def _module_level_set(path: pathlib.Path, name: str) -> set:
    """用 AST 取出模块级 ``name = {"a", "b"}`` 的字符串集合（**不导入模块**）。"""
    tree = ast.parse(_source(path))
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == name for t in node.targets
        ):
            return {e.value for e in node.value.elts if isinstance(e, ast.Constant)}
    raise AssertionError(f"{path.name} 中未找到模块级常量 {name}")


# --------------------------------------------------------------------------
# 1) 纯函数层：用各路由自己的白名单跑共享实现
# --------------------------------------------------------------------------

@pytest.mark.parametrize("router_path,const_name,expected", WHITELISTS)
def test_traversal_name_is_sanitized_under_each_whitelist(router_path, const_name, expected):
    """带 `../` 的文件名在三条路由各自的白名单下都必须被净化成不可穿越的名字。"""
    allowed = _module_level_set(router_path, const_name)

    ext = ensure_supported_extension("../../etc/report.PDF", allowed)

    assert ext == ".pdf"

    out = safe_filename(extension=ext)
    assert "/" not in out and "\\" not in out and ".." not in out


@pytest.mark.parametrize("router_path,const_name,expected", WHITELISTS)
def test_windows_style_traversal_also_sanitized(router_path, const_name, expected):
    """Windows 反斜杠同样要剥掉（原实现只按扩展名切分，不处理目录部分）。"""
    allowed = _module_level_set(router_path, const_name)

    ext = ensure_supported_extension("..\\..\\report.docx", allowed)

    assert ext == ".docx"

    out = safe_filename(extension=ext)
    assert "\\" not in out and ".." not in out


@pytest.mark.parametrize("router_path,const_name,expected", WHITELISTS)
def test_unsupported_extension_still_rejected(router_path, const_name, expected):
    """白名单仍然生效：`.exe` 必须被拒（400）。"""
    allowed = _module_level_set(router_path, const_name)

    with pytest.raises(HTTPException) as excinfo:
        ensure_supported_extension("payload.exe", allowed)

    assert excinfo.value.status_code == 400


# --------------------------------------------------------------------------
# 2) 源码层：实现已并轨、落盘不再用客户端文件名
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "router_path,const_name,expected",
    WHITELISTS,
)
def test_whitelist_members_unchanged(router_path, const_name, expected):
    """🔒 方案 A 的核心证据：白名单成员一个不多、一个不少（零行为变更）。"""
    assert _module_level_set(router_path, const_name) == expected


def test_document_whitelist_is_not_merged_with_others():
    """方案 A 明确要求**不并轨**：文档上传的类型集合保持不变。"""
    document = _module_level_set(DOCUMENT, "SUPPORTED_FILE_TYPES")
    attachment = _module_level_set(ATTACHMENT, "ALLOWED_EXTENSIONS")

    assert document == EXPECTED_DOCUMENT_FILE_TYPES
    assert document != attachment, "白名单已被并轨，违反方案 A"


@pytest.mark.parametrize("router_path", [ATTACHMENT, KNOWLEDGE])
def test_router_no_longer_defines_its_own_extension_parser(router_path):
    """三处 `get_file_extension` 副本必须消失，改由 core.upload_security 提供。"""
    source = _source(router_path)

    assert "def get_file_extension" not in source
    assert "os.path.splitext" not in source
    assert "core.upload_security" in source


@pytest.mark.parametrize("router_path", [ATTACHMENT, KNOWLEDGE])
def test_router_does_not_build_path_from_client_filename(router_path):
    """落盘路径不得再引用 `file.filename` —— 该字符串完全由客户端控制。"""
    source = _source(router_path)

    assert not re.search(r"os\.path\.join\([^)]*file\.filename", source), (
        f"{router_path.name} 仍用客户端文件名拼路径，可被 `../` 穿越"
    )


@pytest.mark.parametrize("router_path", [ATTACHMENT, KNOWLEDGE])
def test_router_still_keeps_client_filename_as_data_field(router_path):
    """原始文件名仍作为**数据字段**保留（展示用），不算回归。"""
    source = _source(router_path)

    assert "filename=file.filename" in source
