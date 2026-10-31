"""T56 回归：`document_router` 的临时文件清理不得把清理失败升级为请求失败。

背景：P-17 的修复（T53）只覆盖了 `knowledge_router`。`document_router.upload_document`
的两处清理仍是**裸 `os.remove`**：

- 成功路径：处理完成后清临时文件；
- `except Exception` 内：失败清临时文件 —— **这一处最关键**，它一旦抛出（同一个文件
  很可能正是刚才失败的那一个），异常会从这个 except 里逸出，把下面准备好的
  `HTTPException(500, "Error processing document: …")` 覆盖成裸 `OSError`。

为什么此前没被发现：T53 的源码锁作用域是 `knowledge_router.py` **单文件**，结构性照不到本文件。

本文件按三层验证，**都不依赖基础设施**（直接以协程调用端点函数 + mock 依赖）：

1. **守卫层** —— `core.upload_security.remove_quietly` 正常删 / 缺失时无操作 /
   `OSError` 时只告警不外抛 / 不传 logger 时不告警；
2. **端点层** —— 处理成功 + 清理失败 → 请求仍成功；处理失败 + 清理失败 → 抛的是
   **本意的 HTTPException(500)** 而非 `PermissionError`；清理成功时临时文件确实被删
   （保证守卫不是「什么都不做」）；
3. **源码层** —— 两个文件都不再出现裸 `os.remove`，且两处清理都委派给守卫。
"""

import logging
from pathlib import Path

import pytest
from fastapi import HTTPException

import core.upload_security as upload_security
from router import document_router

ROUTER_SRC = Path(document_router.__file__).read_text(encoding="utf-8")
ROUTER_DIR = Path(document_router.__file__).parent
SECURITY_SRC = Path(upload_security.__file__).read_text(encoding="utf-8")


class _FakeUpload:
    """最小 `UploadFile` 替身：端点只用到 `.filename`（落盘走被 mock 的 `save_upload`）。"""

    def __init__(self, filename: str = "报告.pdf"):
        self.filename = filename


def _stub_save_upload(target: Path):
    async def _inner(*_args, **_kwargs):
        return str(target)

    return _inner


def _write_target(tmp_path: Path) -> Path:
    target = tmp_path / "uploaded.pdf"
    target.write_bytes(b"%PDF-1.4 stub")
    return target


def _raise_permission_error(_path):
    raise PermissionError("文件仍被占用（模拟 Windows 句柄未释放）")


# --------------------------------------------------------------------------
# 1) 守卫层
# --------------------------------------------------------------------------


def test_remove_quietly_deletes_existing_file(tmp_path):
    """正常路径必须**真的删掉** —— 否则「用不清理来通过其它用例」会成为一条逃生路。"""
    target = tmp_path / "x.pdf"
    target.write_bytes(b"x")

    upload_security.remove_quietly(str(target))

    assert not target.exists()


def test_remove_quietly_is_noop_when_file_missing(tmp_path):
    """文件本就不存在属正常情况（例如 413 在读盘之前就失败）—— 不得抛异常。"""
    upload_security.remove_quietly(str(tmp_path / "never-existed.pdf"))


def test_remove_quietly_swallows_oserror_and_warns(tmp_path, monkeypatch, caplog):
    """🔒 T56 核心：`os.remove` 抛 OSError 时**只告警、绝不外抛**。"""
    target = tmp_path / "busy.pdf"
    target.write_bytes(b"x")
    monkeypatch.setattr(upload_security.os, "remove", _raise_permission_error)

    with caplog.at_level("WARNING"):
        upload_security.remove_quietly(str(target), logger=logging.getLogger("t56"))

    assert target.exists(), "文件不应被删除"
    assert any("临时文件清理失败" in record.message for record in caplog.records), (
        "传了 logger 却没留下告警 —— 清理失败会变成不可观测的静默事件"
    )


def test_remove_quietly_is_silent_without_logger(tmp_path, monkeypatch, caplog):
    """`save_upload` 的失败清理不传 logger —— 那类「文件从未创建」的场景不该刷告警。"""
    target = tmp_path / "busy.pdf"
    target.write_bytes(b"x")
    monkeypatch.setattr(upload_security.os, "remove", _raise_permission_error)

    with caplog.at_level("WARNING"):
        upload_security.remove_quietly(str(target))

    assert not [
        record for record in caplog.records if "临时文件清理失败" in record.message
    ], "未传 logger 时不应产生清理告警"


# --------------------------------------------------------------------------
# 2) 端点层：清理失败不得改变「请求的成败与错误类型」
# --------------------------------------------------------------------------


async def test_success_path_cleanup_failure_still_succeeds(monkeypatch, tmp_path):
    """处理成功 + 清理失败 → 请求**仍然成功**。

    临时文件残留是可接受的代价；让一次已完成的文档处理因为「清不掉临时文件」而报失败，
    是收益为负的行为变更。修复前：成功路径的裸 `os.remove` 抛异常 → 落入 `except Exception`
    → 第二次 `os.remove` 再抛 → 裸 `PermissionError` 逸出。
    """
    target = _write_target(tmp_path)
    monkeypatch.setattr(document_router, "save_upload", _stub_save_upload(target))
    monkeypatch.setattr(
        document_router,
        "process_document_with_docmind",
        lambda **_kwargs: {"success": True, "message": "ok", "document_count": 3},
    )
    monkeypatch.setattr(document_router.os, "remove", _raise_permission_error)

    result = await document_router.upload_document(file=_FakeUpload(), index_name="demo")

    assert result["status"] == "success"
    assert result["document_count"] == 3


async def test_failure_path_cleanup_error_yields_intended_http_500(monkeypatch, tmp_path):
    """🔒 最关键的一条：处理失败 + 清理失败 → 抛的必须是**本意的 500**，而不是 `PermissionError`。

    修复前这里会逸出 `PermissionError`，客户端拿到的错误没有业务语义，
    「文档处理失败」这条本可读的信息被丢弃。
    """
    target = _write_target(tmp_path)
    monkeypatch.setattr(document_router, "save_upload", _stub_save_upload(target))

    def _boom(**_kwargs):
        raise RuntimeError("docmind exploded")

    monkeypatch.setattr(document_router, "process_document_with_docmind", _boom)
    monkeypatch.setattr(document_router.os, "remove", _raise_permission_error)

    with pytest.raises(HTTPException) as excinfo:
        await document_router.upload_document(file=_FakeUpload(), index_name="demo")

    assert excinfo.value.status_code == 500
    assert "Error processing document" in excinfo.value.detail


async def test_failure_path_cleanup_actually_removes_file(monkeypatch, tmp_path):
    """处理失败 + 清理**成功** → 临时文件必须真的被删掉（防「守卫改成 no-op」蒙混）。"""
    target = _write_target(tmp_path)
    monkeypatch.setattr(document_router, "save_upload", _stub_save_upload(target))

    def _boom(**_kwargs):
        raise RuntimeError("docmind exploded")

    monkeypatch.setattr(document_router, "process_document_with_docmind", _boom)

    with pytest.raises(HTTPException):
        await document_router.upload_document(file=_FakeUpload(), index_name="demo")

    assert not target.exists(), "处理失败后留下了临时文件"


async def test_success_path_cleanup_actually_removes_file(monkeypatch, tmp_path):
    """处理成功 + 清理成功 → 临时文件同样必须被删掉。"""
    target = _write_target(tmp_path)
    monkeypatch.setattr(document_router, "save_upload", _stub_save_upload(target))
    monkeypatch.setattr(
        document_router,
        "process_document_with_docmind",
        lambda **_kwargs: {"success": True, "message": "ok", "document_count": 1},
    )

    await document_router.upload_document(file=_FakeUpload(), index_name="demo")

    assert not target.exists()


async def test_declared_failure_result_still_raises_400(monkeypatch, tmp_path):
    """`success: False` 属业务性失败 → 400（且清理失败不得把它变成 500 或 OSError）。"""
    target = _write_target(tmp_path)
    monkeypatch.setattr(document_router, "save_upload", _stub_save_upload(target))
    monkeypatch.setattr(
        document_router,
        "process_document_with_docmind",
        lambda **_kwargs: {"success": False, "message": "解析失败", "document_count": 0},
    )
    monkeypatch.setattr(document_router.os, "remove", _raise_permission_error)

    with pytest.raises(HTTPException) as excinfo:
        await document_router.upload_document(file=_FakeUpload(), index_name="demo")

    assert excinfo.value.status_code == 400
    assert "Document processing failed" in excinfo.value.detail


# --------------------------------------------------------------------------
# 3) 源码层：防未来回退
# --------------------------------------------------------------------------


def test_document_router_has_no_bare_os_remove():
    """🔒 `document_router.py` 内不得再出现裸 `os.remove(`。"""
    assert "os.remove(" not in ROUTER_SRC, "document_router 又出现了裸 os.remove"


def test_both_cleanup_sites_delegate_to_the_guard():
    """两处清理都必须委派给守卫（带 logger，以便失败可观测）。"""
    assert ROUTER_SRC.count("remove_quietly(temp_file_path, logger=logger)") == 2, (
        "清理点数量不是 2 —— 新增 / 删减清理点时应同步复核是否都走了守卫"
    )


def test_no_router_module_has_a_bare_os_remove():
    """🔒 **目录级**锁：`app/router/*.py` 内一律不得再出现裸 `os.remove`。

    T56 的锁 (`test_document_router_has_no_bare_os_remove`) 作用域是 `document_router.py`
    **单文件**，于是同一家族的第三处（`knowledge_router` 的私有实现、
    `attachment_router` 的裸调用）在结构上照不到 —— 终审 §4 中-1 正是这样复发的。
    本锁改成目录级，把整个家族一次钉死；新增路由模块自动纳入。

    作用域仅限 `app/router/`：其它层（`app/service/` 等）尚无同类清理点，
    不在此锁内，避免过度约束。
    """
    offenders = sorted(
        path.name
        for path in ROUTER_DIR.glob("*.py")
        if "os.remove(" in path.read_text(encoding="utf-8")
    )
    assert not offenders, (
        f"以下路由模块又出现裸 os.remove：{offenders} —— "
        "清理一律走 core.upload_security.remove_quietly"
    )


def test_guard_is_public_and_swallows_oserror():
    """守卫本身：公共名 `remove_quietly`，且 `os.remove` 被 `except OSError` 包住。"""
    assert "def remove_quietly(" in SECURITY_SRC, "守卫未提升为公共名"
    assert "_remove_quietly" not in SECURITY_SRC, "仍残留私有旧名"

    body = SECURITY_SRC.split("def remove_quietly(", 1)[1].split("async def save_upload", 1)[0]
    assert "os.remove(" in body, "守卫内没有 os.remove，断言失去意义"
    assert "except OSError" in body, "os.remove 未被 except OSError 包住"
