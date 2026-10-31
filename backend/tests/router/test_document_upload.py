"""T03 回归测试：文档上传的路径穿越、文件类型与大小校验。

背景（事实 F-02）：``document_router.upload_document`` 原以
``f"/tmp/{file.filename}"`` 落盘 —— 文件名完全由客户端控制，``../../`` 片段可以把
文件写出 ``/tmp``；该接口同时没有大小限制与扩展名白名单。

被测逻辑已抽到 ``core/upload_security``：它不依赖 milvus / ES / docmind，
因此本文件可以**脱离基础设施**直接运行（符合 T03 的「无基础设施即可验证」口径）。
"""

import asyncio

import pytest
from fastapi import HTTPException

from core.upload_security import (
    ensure_supported_extension,
    read_upload_with_limit,
    safe_filename,
    sanitize_extension,
)

SUPPORTED = {".pdf", ".docx", ".xlsx", ".xls", ".txt"}

# 各类路径穿越 / 目录注入写法
TRAVERSAL_NAMES = [
    "../../etc/passwd",
    "..\\..\\win.ini",
    "a/b/c.txt",
    "/etc/shadow.pdf",
    "....//....//evil.pdf",
]


class _FakeUpload:
    """最小 ``UploadFile`` 替身：只需实现 ``async read(n)``。"""

    def __init__(self, payload: bytes):
        self._payload = payload
        self._offset = 0

    async def read(self, size: int = -1) -> bytes:
        if size is None or size < 0:
            chunk = self._payload[self._offset:]
            self._offset = len(self._payload)
            return chunk
        chunk = self._payload[self._offset:self._offset + size]
        self._offset += len(chunk)
        return chunk


@pytest.mark.parametrize("bad_name", TRAVERSAL_NAMES)
def test_safe_filename_cannot_escape_directory(bad_name):
    """落盘名中不得出现任何路径分隔符或 ``..``，否则可写出目标目录。"""
    out = safe_filename(bad_name)

    assert "/" not in out
    assert "\\" not in out
    assert ".." not in out


@pytest.mark.parametrize("bad_name", TRAVERSAL_NAMES)
def test_safe_filename_never_embeds_client_name(bad_name):
    """客户端文件名不得以任何形式进入落盘名（前缀 / 后缀都不行）。"""
    out = safe_filename(bad_name)

    # 去掉扩展名后应当恰好是一个 uuid4
    stem = out.rsplit(".", 1)[0] if "." in out else out
    assert len(stem) == 36, f"{bad_name!r} -> {out!r} 的 uuid 段长度异常"


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("report.PDF", ".pdf"),
        ("../../etc/Passwd.PdF", ".pdf"),
        ("..\\..\\win.INI", ".ini"),
        ("no_extension", ""),
        (None, ""),
    ],
)
def test_sanitize_extension(raw, expected):
    assert sanitize_extension(raw) == expected


def test_unsupported_extension_is_rejected():
    with pytest.raises(HTTPException) as excinfo:
        ensure_supported_extension("payload.exe", SUPPORTED)

    assert excinfo.value.status_code == 400


def test_supported_extension_is_accepted():
    assert ensure_supported_extension("policy.docx", SUPPORTED) == ".docx"


def test_traversal_name_is_judged_by_stripped_extension():
    """``..\\..\\report.PDF`` 应被判为合法的 ``.pdf``，而不是被当成路径。"""
    assert ensure_supported_extension("..\\..\\report.PDF", SUPPORTED) == ".pdf"


def test_read_upload_within_limit():
    payload = b"x" * 1024

    assert asyncio.run(read_upload_with_limit(_FakeUpload(payload), 2048)) == payload


def test_read_upload_over_limit_is_rejected():
    payload = b"x" * 4096

    with pytest.raises(HTTPException) as excinfo:
        asyncio.run(read_upload_with_limit(_FakeUpload(payload), 1024))

    assert excinfo.value.status_code == 413


def test_read_upload_boundary_exactly_at_limit():
    """边界：恰好等于上限应放行，超出 1 字节即拒绝。"""
    assert asyncio.run(read_upload_with_limit(_FakeUpload(b"x" * 1024), 1024)) == b"x" * 1024

    with pytest.raises(HTTPException):
        asyncio.run(read_upload_with_limit(_FakeUpload(b"x" * 1025), 1024))
