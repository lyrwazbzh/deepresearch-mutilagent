# Copyright © 2026 深圳市深维智见教育科技有限公司 版权所有
# 未经授权，禁止转售或仿制。

"""研究取消协议（从 `router/research_router.py` 迁出的中立位，见 tickets.md T68）。

归属理由与 `core/serialization.py`（T15）同型：取消协议此前归 `research_router`
所有，于是**服务层必须反向 import router** 才能用它
（`service/deep_research_v2/graph.py`）—— 一个 HTTP 层模块成了业务流水线的基础设施，
两模块之间还构成了循环依赖。本模块与两侧都无关：router 与 service 都只依赖它。

**本模块刻意没有 ImportError 兜底**。迁出前 `graph.py` 的写法是
`try: from router.research_router import ...` / `except ImportError: 把取消判定降级为
「恒返回 False」` —— 即导入一失败就**静默地永不取消**（fail-open），而同一个兜底
还把两模块间的循环依赖一起掩盖了：导入失败时流水线会静默跑到天亮。
现在「取消」只有一条可用路径，本模块不可用就是**导入期硬失败**（loud），
不再伪装成「未取消」。

边界（不在本模块职责内，记录在案）：Redis 不可达时 `RedisCache.get` 会吞掉异常并
返回 None，于是判定表现为「未取消」—— 这是 `RedisCache` 的既有全局行为（读写都
如此），属另一条待议项，不在 T68 范围内。
"""

import logging

from .redis_client import cache

logger = logging.getLogger(__name__)

#: 取消标志的 Redis key 前缀
CANCEL_KEY_PREFIX = "research:cancel:"

#: 取消标志有效期（秒）—— 与迁出前 router 里的 `expire=300` 逐字一致
CANCEL_TTL_SECONDS = 300


def _cancel_key(session_id: str) -> str:
    """取消标志在 Redis 中的 key。"""
    return f"{CANCEL_KEY_PREFIX}{session_id}"


def request_cancel(session_id: str) -> bool:
    """请求取消某个研究会话（写取消标志）。

    Returns:
        Redis 是否写入成功。写入失败时记 warning 并返回 False —— 调用方不应把
        「写失败」当成「取消已受理」。
    """
    ok = cache.set(
        _cancel_key(session_id), {"cancelled": True}, expire=CANCEL_TTL_SECONDS
    )
    if not ok:
        logger.warning("Failed to persist cancel flag for session: %s", session_id)
    return ok


def is_cancelled(session_id: str) -> bool:
    """该研究会话是否已被请求取消。"""
    result = cache.get(_cancel_key(session_id))
    return result is not None and result.get("cancelled", False)


def clear_cancel_flag(session_id: str) -> None:
    """清除取消标志（研究开始时调用）。"""
    cache.delete(_cancel_key(session_id))
