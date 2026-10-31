"""T44 回归测试：上传落盘生命周期并轨（方案 B）。

背景：`document_router` / `attachment_router` / `knowledge_router` 三处的上传落盘是
三段重复代码（`read_upload_with_limit` → `open/write` → 异常映射），且**失败清理策略
分叉** —— 只有 document 分支会在失败后删掉半截文件，另两处会留下孤儿文件。
`save_upload` 把这五件事（建目录 → 限长读取 → 写盘 → 异常映射 → 失败清理）收敛到
`core.upload_security`，三入口共用。

本文件按三层验证，**都不依赖基础设施**：

1. **行为层** —— 直接对 `save_upload` 断言：成功落盘、**写入失败时半截文件被清理**、
   超限抛 413 且不留文件、目标目录不存在时自动创建；
2. **源码层** —— 三个路由都改为调用 `save_upload`，且**不再自持写盘代码**，
   这是「生命周期确实并轨到三处」的证据；
3. 变异检查见 PR 描述（撤掉清理逻辑 → 本文件必须失败）。
"""

import builtins
import pathlib
import re

import pytest
from fastapi import HTTPException

import core.upload_security as upload_security

ROUTER_DIR = pathlib.Path(__file__).resolve().parents[2] / "app" / "router"

ROUTERS = [
    ROUTER_DIR / "document_router.py",
    ROUTER_DIR / "attachment_router.py",
    ROUTER_DIR / "knowledge_router.py",
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


# --------------------------------------------------------------------------
# 1) 行为层
# --------------------------------------------------------------------------

async def test_save_upload_writes_file_and_returns_path(tmp_path):
    """成功路径：内容完整落盘，路径在目标目录内、扩展名正确、名字由服务端生成。"""
    payload = b"hello world"

    path = await upload_security.save_upload(
        _FakeUpload(payload), str(tmp_path), extension=".pdf"
    )

    written = pathlib.Path(path)
    assert written.parent == tmp_path
    assert written.suffix == ".pdf"
    assert written.read_bytes() == payload
    # 落盘名必须由服务端生成（uuid），不得嵌客户端文件名
    assert len(written.stem) == 36


async def test_save_upload_creates_dest_dir_when_missing(tmp_path):
    """目标目录不存在时自动创建（并轨前只有 document 分支显式 makedirs）。"""
    target = tmp_path / "nested" / "dir"

    path = await upload_security.save_upload(
        _FakeUpload(b"a"), str(target), extension=".txt"
    )

    assert target.is_dir()
    assert pathlib.Path(path).exists()


async def test_save_upload_cleans_partial_file_on_write_failure(tmp_path, monkeypatch):
    """🔒 T44 核心断言：写入中途失败时，**半截文件必须被删掉**。

    并轨前只有 document 分支做这件事；attachment / knowledge 会留下孤儿文件。
    本用例把并轨后的统一行为钉住。

    实现方式：把 `upload_security` 模块内的 `open` 名字换成替身 —— 它先**真的**落一个
    半截文件，再在 `write()` 时失败，从而精确复现「写了一半才崩」的场景。
    """
    real_open = builtins.open
    written = []

    class _HalfWritingFile:
        def __init__(self, path):
            self._path = path

        def __enter__(self):
            # 真的落盘，否则「清理」这一步将无对象可清，测试会失去判别力
            with real_open(self._path, "wb") as handle:
                handle.write(b"partial")
            written.append(self._path)
            return self

        def write(self, data):
            raise OSError("simulated disk full")

        def __exit__(self, *exc_info):
            return False

    def _failing_open(path, mode="r", *args, **kwargs):
        if mode == "wb":
            return _HalfWritingFile(path)
        return real_open(path, mode, *args, **kwargs)

    # 只替换模块内的 `open` 名字（函数 globals 会先解析到这里），不动 builtins
    monkeypatch.setattr(upload_security, "open", _failing_open, raising=False)

    with pytest.raises(HTTPException) as excinfo:
        await upload_security.save_upload(
            _FakeUpload(b"x" * 64), str(tmp_path), extension=".txt"
        )

    assert excinfo.value.status_code == 500
    assert written, "替身未真正落盘，本用例失去判别力"
    assert list(tmp_path.iterdir()) == [], "写入失败后留下了半截文件（未清理）"


async def test_save_upload_over_limit_raises_413_and_leaves_no_file(tmp_path):
    """超限仍在**写盘之前**就失败：抛 413，且目录里不留任何文件。"""
    with pytest.raises(HTTPException) as excinfo:
        await upload_security.save_upload(
            _FakeUpload(b"x" * 128), str(tmp_path), extension=".txt", max_bytes=64
        )

    assert excinfo.value.status_code == 413
    assert list(tmp_path.iterdir()) == []


async def test_save_upload_does_not_swallow_413_into_500(tmp_path):
    """413 必须原样透出 —— 不能被兜底 except 转成 500（并轨前的三处都刻意区分）。"""
    with pytest.raises(HTTPException) as excinfo:
        await upload_security.save_upload(
            _FakeUpload(b"y" * 256), str(tmp_path), extension=".txt", max_bytes=8
        )

    assert excinfo.value.status_code != 500


# --------------------------------------------------------------------------
# 2) 源码层：三入口确实共用同一生命周期
# --------------------------------------------------------------------------

@pytest.mark.parametrize("router_path", ROUTERS)
def test_router_delegates_to_shared_save_upload(router_path):
    """三个路由都必须调用共享助手，而不是各自维护一套落盘代码。"""
    source = router_path.read_text(encoding="utf-8")

    assert "save_upload(" in source, f"{router_path.name} 未使用共享落盘助手"


@pytest.mark.parametrize("router_path", ROUTERS)
def test_router_no_longer_owns_the_write_lifecycle(router_path):
    """🔒 并轨证据：路由内不得再出现写盘、限长读取或自行建目录的代码。"""
    source = router_path.read_text(encoding="utf-8")

    assert not re.search(r'open\([^)]*,\s*"wb"\)', source), (
        f"{router_path.name} 仍自行写盘，落盘生命周期未并轨"
    )
    assert "read_upload_with_limit" not in source, (
        f"{router_path.name} 仍自行限长读取，落盘生命周期未并轨"
    )
    assert "safe_filename" not in source, (
        f"{router_path.name} 仍自行生成落盘名，落盘生命周期未并轨"
    )
