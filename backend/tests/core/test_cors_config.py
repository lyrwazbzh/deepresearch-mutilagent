"""T06 回归测试：CORS 来源必须来自显式白名单，且与凭据开关不冲突。

背景（事实 F-05）：`app_main.py` 原先同时设置 `allow_origins=["*"]` 与
`allow_credentials=True`。该组合被浏览器规范禁止 —— 此时 `Access-Control-Allow-Origin`
不会返回 `*`，带凭据的跨域请求实际失效；同时「允许所有来源」在生产环境不可接受。

本测试只导入 `core.cors`（纯标准库），不触碰 `app_main`，因此无需任何基础设施。
"""

import pathlib

import pytest

from core.cors import (
    CORS_ORIGINS_ENV,
    WILDCARD_ORIGIN,
    build_cors_kwargs,
    is_production_env,
    parse_cors_origins,
)

BACKEND_DIR = pathlib.Path(__file__).resolve().parents[2]


def test_wildcard_origin_forces_credentials_off():
    """通配来源 + 凭据开关不能同时为真 —— 这正是原实现的问题所在。"""
    kwargs = build_cors_kwargs(WILDCARD_ORIGIN)

    assert kwargs["allow_origins"] == [WILDCARD_ORIGIN]
    assert kwargs["allow_credentials"] is False


def test_explicit_origin_list_is_parsed_and_credentials_allowed():
    kwargs = build_cors_kwargs("https://a.com,https://b.com")

    assert kwargs["allow_origins"] == ["https://a.com", "https://b.com"]
    assert kwargs["allow_credentials"] is True


def test_blank_and_trailing_commas_are_dropped():
    assert parse_cors_origins(" http://a.com , http://b.com , ") == [
        "http://a.com",
        "http://b.com",
    ]
    assert parse_cors_origins("") == []
    assert parse_cors_origins(None) == []


def test_empty_origins_in_production_fails_startup():
    """生产环境留空必须直接失败，而不是静默放开成任意来源。"""
    with pytest.raises(RuntimeError, match=CORS_ORIGINS_ENV):
        build_cors_kwargs("", production=True)


def test_empty_origins_outside_production_falls_back_to_wildcard_without_credentials():
    """非生产环境留空 → 退回 `*` 方便本地调试，但仍禁用凭据。"""
    kwargs = build_cors_kwargs("", production=False)

    assert kwargs["allow_origins"] == [WILDCARD_ORIGIN]
    assert kwargs["allow_credentials"] is False


@pytest.mark.parametrize("env,expected", [("production", True), ("prod", True), ("PRODUCTION", True), ("development", False), ("test", False), ("", False)])
def test_production_env_detection(env, expected):
    assert is_production_env(env) is expected


def test_methods_and_headers_stay_open():
    """方法与请求头仍放开，本票只收紧来源与凭据的组合。"""
    kwargs = build_cors_kwargs("https://a.com")

    assert kwargs["allow_methods"] == ["*"]
    assert kwargs["allow_headers"] == ["*"]


def test_app_main_no_longer_hardcodes_wildcard_with_credentials():
    """结构断言：`app_main.py` 里不得再出现「通配来源 + 允许凭据」的硬编码组合。"""
    source = (BACKEND_DIR / "app" / "app_main.py").read_text(encoding="utf-8")

    assert 'allow_origins=["*"]' not in source
    assert "build_cors_kwargs()" in source


def test_env_example_documents_the_variable():
    """`.env.example` 必须列出该变量，否则你不知道要配什么。"""
    content = (BACKEND_DIR / ".env.example").read_text(encoding="utf-8")

    assert CORS_ORIGINS_ENV in content
