# Copyright © 2026 深圳市深维智见教育科技有限公司 版权所有
# 未经授权，禁止转售或仿制。

"""安全响应头：Content-Security-Policy 的策略值与路径豁免规则。

**为什么独立成模块**：与 ``core/cors.py`` 同一理由 —— ``app/app_main.py`` 一被导入就会
连带拉起全部路由、模型与数据库引擎（``DB_AUTO_CREATE=1`` 时还会建表），其中的策略无法
在「无基础设施」的测试里验证。本模块只依赖标准库，可被单测直接导入。

事实来源：T37 —— 前端把 JWT 存在 ``localStorage``
（``frontend/src/api/request/plugins/auth.ts`` 读取），该方式对 XSS 完全无防护，
任意一次 XSS 即可窃取 Token。T37 的裁决（方案 A）为「不动存储方式，改为补防护」；
本模块是其中的**后端一半**，另一半是前端把 ``localStorage`` 访问点收敛到单一模块。

⚠️ **边界（勿误读为已根治 XSS）**：CSP 是**按来源（origin）生效**的响应头。本项目的 SPA
由前端自己的服务器提供、不经过本后端，因此本模块**只覆盖 API 响应**；SPA 页面自身的 CSP
必须由托管它的一方（nginx / 静态托管）设置。本模块的实际价值是让 API 响应不会被当作
文档加载 —— 禁止被 iframe 嵌入、禁止改写 base、禁止 object 与表单外发，从而封掉
「把 API 端点当页面套壳」这一整类利用面。
"""

from typing import Dict, Tuple

# 交互式文档：Swagger UI / ReDoc 需要从 jsdelivr CDN 加载脚本与样式，且自带内联脚本，
# 严格 CSP 会直接把它们打坏。这些路径是开发工具、不承载业务数据，故整体豁免。
DOCS_PATH_PREFIXES: Tuple[str, ...] = ("/docs", "/redoc")
DOCS_PATH_EXACT: Tuple[str, ...] = ("/openapi.json",)

# 面向 API 响应的严格策略：
# - default-src 'none'：API 只返回 JSON，不需要任何子资源
# - base-uri 'none'：禁止 <base> 改写相对地址
# - object-src 'none'：禁止 <object> / <embed>
# - form-action 'none'：禁止表单外发
# - frame-ancestors 'none'：禁止被任何页面 iframe 嵌入（比 X-Frame-Options: DENY 更细）
CONTENT_SECURITY_POLICY = (
    "default-src 'none'; "
    "base-uri 'none'; "
    "object-src 'none'; "
    "form-action 'none'; "
    "frame-ancestors 'none'"
)


def is_docs_path(path: str) -> bool:
    """是否为交互式文档路径，需要豁免 CSP。

    用**路径段**边界判断而非裸 ``startswith``：``/docsx`` 不是文档路径，
    不应被豁免（裸 ``startswith("/docs")`` 会把它误判为文档，留出绕过缺口）。
    """
    if path in DOCS_PATH_EXACT:
        return True
    return any(path == prefix or path.startswith(prefix + "/") for prefix in DOCS_PATH_PREFIXES)


def security_headers_for(path: str) -> Dict[str, str]:
    """返回该请求路径应附加的安全响应头；文档路径返回空字典（豁免）。"""
    if is_docs_path(path):
        return {}
    return {"Content-Security-Policy": CONTENT_SECURITY_POLICY}
