"""T37 回归测试：API 响应必须带 CSP，交互式文档路径豁免。

背景：T37 是决策票（用户已授权 AI 裁决），选方案 A —— 不动 JWT 的 ``localStorage``
存储方式，改为补防护。防护分两半：前端把本地存储访问点收敛到单一模块，后端补
``Content-Security-Policy`` 响应头。本文件锁定后半。

本测试只导入 ``core.security_headers``（纯标准库），不触碰 ``app_main``，
因此无需任何基础设施；策略内容与豁免边界都在这里锁死。
"""

import pathlib

import pytest

from core.security_headers import (
    CONTENT_SECURITY_POLICY,
    DOCS_PATH_EXACT,
    DOCS_PATH_PREFIXES,
    is_docs_path,
    security_headers_for,
)

APP_MAIN_PATH = (
    pathlib.Path(__file__).resolve().parents[2] / "app" / "app_main.py"
)


def test_csp_is_attached_to_a_normal_path():
    headers = security_headers_for("/hello")

    assert headers["Content-Security-Policy"] == CONTENT_SECURITY_POLICY


def test_only_the_csp_header_is_added():
    """本票只承诺补 CSP，不顺手塞其它安全头（避免无票变更）。"""
    assert list(security_headers_for("/hello")) == ["Content-Security-Policy"]


@pytest.mark.parametrize(
    "path",
    ["/docs", "/docs/oauth2-redirect", "/redoc", "/redoc/anything", "/openapi.json"],
)
def test_docs_paths_are_exempt(path):
    """Swagger UI / ReDoc 需要 CDN 与内联脚本，严格 CSP 会打坏它们。"""
    assert is_docs_path(path)
    assert security_headers_for(path) == {}


@pytest.mark.parametrize("path", ["/docsx", "/docsmith", "/redocument", "/openapi.jsonx", "/"])
def test_paths_sharing_a_prefix_are_not_exempt(path):
    """路径段边界：仅前缀相同不构成豁免，否则会留下绕过缺口。"""
    assert not is_docs_path(path)
    assert security_headers_for(path)["Content-Security-Policy"] == CONTENT_SECURITY_POLICY


@pytest.mark.parametrize(
    "directive",
    [
        "default-src 'none'",
        "base-uri 'none'",
        "object-src 'none'",
        "form-action 'none'",
        "frame-ancestors 'none'",
    ],
)
def test_policy_contains_each_hardening_directive(directive):
    """这五条就是本次防护的实际内容，掉任何一条都应报警。"""
    assert directive in CONTENT_SECURITY_POLICY


def test_docs_constants_are_not_wildcards():
    """豁免清单必须是显式路径，不得退化成通配。"""
    assert "*" not in "".join(DOCS_PATH_PREFIXES) + "".join(DOCS_PATH_EXACT)


def test_app_main_actually_registers_the_middleware():
    """结构回归锁：中间件必须真的挂在 ``app_main`` 上。

    本文件其余用例只覆盖纯函数 —— 若有人删掉中间件注册，它们仍会全绿
    （实测：删掉后 ``pytest tests -q`` 依然 283 passed）。这里沿用
    ``tests/core/test_cors_config.py`` 对源码做文本断言的既有做法，把「是否接线」也锁上。
    """
    source = APP_MAIN_PATH.read_text(encoding="utf-8")

    assert '@app.middleware("http")' in source
    assert "security_headers_for" in source


@pytest.mark.parametrize("weakening", ["unsafe-inline", "unsafe-eval", "*"])
def test_policy_contains_no_weakening_token(weakening):
    """策略不得含降级令牌 —— 防止将来「为了让 /docs 能跑」顺手放宽主策略
    （正确做法是走 ``DOCS_PATH_*`` 豁免，而不是削弱全局策略）。"""
    assert weakening not in CONTENT_SECURITY_POLICY
