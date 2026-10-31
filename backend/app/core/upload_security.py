# Copyright © 2026 深圳市深维智见教育科技有限公司 版权所有
# 未经授权，禁止转售或仿制。

"""文件上传安全工具：文件名净化、扩展名校验、大小限制、落盘生命周期。

**为什么独立成模块**：``router/document_router.py`` 会连带引入 milvus / ES / docmind
等重依赖，导致这些纯粹的上传校验逻辑无法在「无基础设施」的 CI 中验证（见 T27 口径）。
本模块只依赖标准库 + FastAPI，可被单测直接导入。

事实来源：T03 / 事实 F-02 —— 原实现用 ``f"/tmp/{file.filename}"`` 落盘，
文件名完全由客户端控制，``../../`` 片段足以把文件写出 ``/tmp``。
"""

import logging
import os
import uuid
from typing import Iterable, Optional

from fastapi import HTTPException
from starlette.status import HTTP_400_BAD_REQUEST, HTTP_500_INTERNAL_SERVER_ERROR

# 分块读取上传内容时的块大小
READ_CHUNK_BYTES = 1024 * 1024

# 413 的名字在 starlette 新旧版本间改过（REQUEST_ENTITY_TOO_LARGE → CONTENT_TOO_LARGE），
# 而 requirements 只给了 `fastapi>=0.104.0` 这样的下界。为避免绑定某个 starlette 版本，
# 这里直接用数值。
HTTP_413_CONTENT_TOO_LARGE = 413

# 单文件大小上限：三个上传入口（document / attachment / knowledge）共用同一口径。
# 取常量即可，不必做成可配置项。
MAX_UPLOAD_BYTES = 50 * 1024 * 1024


def sanitize_extension(filename: Optional[str]) -> str:
    """从客户端文件名中取出扩展名：小写、含点。

    先剥掉客户端可能塞入的目录部分（**同时兼容 POSIX 与 Windows 分隔符**），
    再做扩展名提取。否则 ``..\\..\\x.pdf`` 这类名字在后续拼接路径时仍然危险。
    """
    basename = (filename or "").replace("\\", "/").rsplit("/", 1)[-1]
    return os.path.splitext(basename)[1].lower()


def safe_filename(
    original_filename: Optional[str] = None,
    *,
    extension: Optional[str] = None,
) -> str:
    """生成服务端**完全可控**的落盘文件名：``<uuid><扩展名>``。

    刻意不保留客户端提供的文件名（哪怕只当后缀或前缀）：该字符串不受任何约束，
    ``../`` 片段足以把文件写出目标目录。原始文件名若需展示，请作为**数据字段**入库，
    不要进入路径。

    ``original_filename`` 仅用于推导扩展名（当 ``extension`` 未显式给出时）。
    """
    ext = extension if extension is not None else sanitize_extension(original_filename)
    return f"{uuid.uuid4()}{ext}"


def ensure_supported_extension(filename: Optional[str], allowed: Iterable[str]) -> str:
    """校验扩展名是否在白名单内；不支持时抛 400。

    返回规范化后的扩展名，供调用方直接用于生成落盘文件名。
    """
    extension = sanitize_extension(filename)
    allowed_set = set(allowed)
    if extension not in allowed_set:
        raise HTTPException(
            status_code=HTTP_400_BAD_REQUEST,
            detail=(
                f"不支持的文件类型: {extension or '(无扩展名)'}。"
                f"支持的类型: {', '.join(sorted(allowed_set))}"
            ),
        )
    return extension


async def read_upload_with_limit(upload, max_bytes: int) -> bytes:
    """按块累计读取上传内容；累计超过 ``max_bytes`` 立即抛 413。

    之所以不写成 ``content = await upload.read()`` 再比长度：那样大小校验发生在读完之后，
    内存已被占满，**拦不住超大文件**。本函数在超过上限的**那一刻**就失败，
    因此峰值内存被 ``max_bytes`` 限住。

    注意：返回值仍是完整字节串，**峰值内存 ≈ 文件大小**（上限内），
    调用方无法避免整体驻留；要真正做到 O(1) 内存需改成边读边写盘。
    """
    chunks = []
    total = 0
    while True:
        chunk = await upload.read(READ_CHUNK_BYTES)
        if not chunk:
            break
        total += len(chunk)
        if total > max_bytes:
            raise HTTPException(
                status_code=HTTP_413_CONTENT_TOO_LARGE,
                detail=f"文件超过大小上限 {max_bytes // (1024 * 1024)} MB",
            )
        chunks.append(chunk)
    return b"".join(chunks)


def remove_quietly(path: str, *, logger: Optional[logging.Logger] = None) -> None:
    """尽力删除文件；删不掉也**绝不外抛**（可选地留一条告警）。

    本函数只用在**清理路径**上：那里再抛异常会掩盖原始错误。两类调用方对「删不掉」
    的期望不同，故 logger 是**可选**的：

    - `save_upload` 的失败清理：文件可能**从未创建**（例如 413 在读盘之前就失败），
      属正常情况，传 logger 只会制造噪声 → 不传，静默。
    - 路由端点 / 后台任务的清理（`document_router` / `knowledge_router` /
      `attachment_router`）：文件本该存在却删不掉，属异常（Windows 下句柄仍被占用 →
      `PermissionError`）→ 传 logger，留下可观测痕迹。关键性质是二者都不让清理失败
      升级为请求失败乃至进程死亡（T53 / P-17；T56 / 终审 §4 N3；T63 / 终审 §4 中-1 ——
      `attachment_router` 正是原先**漏掉**的第三处）。
    """
    try:
        os.remove(path)
    except OSError as exc:
        if logger is not None:
            logger.warning("临时文件清理失败，已保留 %s：%s", path, exc)


async def save_upload(
    upload,
    dest_dir: str,
    *,
    extension: str,
    max_bytes: int = MAX_UPLOAD_BYTES,
) -> str:
    """把上传内容写入 ``dest_dir`` 下的服务端命名文件，返回落盘路径。

    把「建目录 → 限长读取 → 写盘 → 异常映射 → 失败清理」并轨到一处，
    供 document / attachment / knowledge 三个上传入口共用（T44 方案 B）。

    并轨前三个入口的**失败清理策略是分叉的**：只有 document 分支会在失败后删掉
    半截文件，attachment / knowledge 会留下孤儿文件。本函数统一保证
    「**任何失败都不留残留文件**」。

    ``extension`` 必须来自**已通过白名单校验**的返回值（各入口白名单不同，故不在此
    重复校验），例如 ``ensure_supported_extension(file.filename, ALLOWED)``。

    异常语义：
    - 累计读取超过 ``max_bytes`` → 原样透出 413（不被下面的兜底转成 500）；
    - 写盘期间其它任何异常 → 删掉半截文件后抛 500（``文件保存失败: ...``）。
    """
    os.makedirs(dest_dir, exist_ok=True)
    file_path = os.path.join(dest_dir, safe_filename(extension=extension))
    try:
        content = await read_upload_with_limit(upload, max_bytes)
        with open(file_path, "wb") as buffer:
            buffer.write(content)
    except HTTPException:
        # 413 等业务异常原样透出，不要被下面的兜底转成 500
        remove_quietly(file_path)
        raise
    except Exception as e:
        remove_quietly(file_path)
        raise HTTPException(
            status_code=HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"文件保存失败: {str(e)}",
        )
    return file_path
