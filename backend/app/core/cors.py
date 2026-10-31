# Copyright © 2026 深圳市深维智见教育科技有限公司 版权所有
# 未经授权，禁止转售或仿制。

"""CORS 来源白名单的解析与中间件参数构造。

**为什么独立成模块**：``app/app_main.py`` 一被导入就会连带拉起全部路由、模型与数据库引擎
（且在 ``DB_AUTO_CREATE=1`` 时触发 ``Base.metadata.create_all``），因此其中的 CORS 构造逻辑
无法在「无基础设施」的测试里验证。本模块只依赖标准库，可被单测直接导入
（与 ``core/upload_security.py`` 同一做法）。

事实来源：T06 / 事实 F-05 —— 原实现同时设置 ``allow_origins=["*"]`` 与
``allow_credentials=True``。该组合被浏览器规范禁止：此时 ``Access-Control-Allow-Origin``
不会返回 ``*``，带凭据的跨域请求实际失效；同时「允许所有来源」在生产环境不可接受。

说明：本项目的登录态走 ``Authorization: Bearer`` 头（见 ``frontend/src/api/request/plugins/auth.ts``），
不依赖 Cookie，因此 ``allow_credentials=False`` 不会影响前端正常调用。
"""

import logging
import os
from typing import Dict, List, Optional

# 读取来源白名单的环境变量名（逗号分隔）
CORS_ORIGINS_ENV = "CORS_ALLOW_ORIGINS"

# 通配来源。与 allow_credentials=True 互斥。
WILDCARD_ORIGIN = "*"

# 视为「生产环境」的 ENV 取值；其余（development / test / ...）按非生产处理。
_PRODUCTION_ENV_VALUES = frozenset({"production", "prod"})

logger = logging.getLogger(__name__)


def parse_cors_origins(raw: Optional[str]) -> List[str]:
    """把逗号分隔的来源白名单拆成列表：去首尾空白、丢弃空项。

    ``"a, b,"`` -> ``["a", "b"]``；``None`` / ``""`` -> ``[]``。
    """
    if not raw:
        return []
    return [origin.strip() for origin in raw.split(",") if origin.strip()]


def is_production_env(env: Optional[str] = None) -> bool:
    """判断是否为生产环境。``env`` 为空时读 ``ENV``（与 observability 的同一约定）。"""
    value = env if env is not None else os.getenv("ENV", "development")
    return (value or "").strip().lower() in _PRODUCTION_ENV_VALUES


def build_cors_kwargs(
    raw_origins: Optional[str] = None,
    *,
    production: Optional[bool] = None,
) -> Dict[str, object]:
    """构造 ``CORSMiddleware`` 的参数。

    规则：

    1. 来源取自 ``CORS_ALLOW_ORIGINS``（逗号分隔的显式白名单）。
    2. 来源含通配 ``*`` 时**强制** ``allow_credentials=False``，并打警告日志。
    3. 生产环境来源为空 -> 抛 ``RuntimeError``，让启动直接失败（与 T02 的 JWT 校验同一风格），
       而不是静默放开成任意来源。
    4. 非生产环境来源为空 -> 退回 ``*`` 方便本地调试，仍禁用凭据。

    ``raw_origins`` / ``production`` 省略时才回落到环境变量，便于单测注入。
    """
    if raw_origins is None:
        raw_origins = os.getenv(CORS_ORIGINS_ENV)
    if production is None:
        production = is_production_env()

    origins = parse_cors_origins(raw_origins)

    if not origins:
        if production:
            raise RuntimeError(
                f"生产环境必须显式配置 {CORS_ORIGINS_ENV}（逗号分隔的来源白名单）。"
                "留空会使跨域来源失去限制，因此这里直接终止启动。"
            )
        origins = [WILDCARD_ORIGIN]

    allow_credentials = WILDCARD_ORIGIN not in origins
    if not allow_credentials:
        logger.warning(
            "CORS 来源含通配 %r：已强制 allow_credentials=False（浏览器规范禁止二者并存，"
            "同时设置会让带凭据请求实际失效）。生产环境请把 %s 设为具体来源。",
            WILDCARD_ORIGIN,
            CORS_ORIGINS_ENV,
        )

    return {
        "allow_origins": origins,
        "allow_credentials": allow_credentials,
        "allow_methods": ["*"],
        "allow_headers": ["*"],
    }
