"""T47 回归测试：serper 密钥缺失时「搜索调用」显式报错，而不是带空 X-API-KEY 静默发请求。

背景：`service/config.py` 的注释如实记录过三键缺失行为**不一致** —— `api_key` /
`default_dataset_id` 由调用方显式抛错（响亮失败），而 `serper_api_key` 为空时
`WebSearchService.search()` 会照常 POST 一个空 `X-API-KEY` 的请求，拿到第三方
401/403 后又被当成一次普通的搜索失败。T47 方案 C 把它改成：
**需要 serper 时（即真正调用 `search()`）立刻抛明确的 ValueError**。

本测试**不发网络请求**：缺密钥的用例在建立连接之前就已抛错（并由 `HTTPSConnection`
替身断言「确实没发起连接」）；有密钥的用例只断言构造与守卫放行。

设计钉桩：守卫在 `search()` 而**非** `__init__`。因为本服务在 chat 路由里由共用依赖
`get_services()` 构造，若在构造期报错，会把会话创建等**与检索无关**的端点一起拖垮
（那是方案 B 的语义，会改变可启动性，不是本票要的）。
"""

import http.client

import pytest

from service.web_search_service import WebSearchService


def _clear_env_key(monkeypatch):
    monkeypatch.delenv("SERPER_API_KEY", raising=False)


def test_search_without_any_key_raises(monkeypatch):
    """无显式密钥、环境变量也为空 → search() 必须抛错。这是本票的核心断言。"""
    _clear_env_key(monkeypatch)
    service = WebSearchService()  # 构造期不报错：serper 是可选能力

    with pytest.raises(ValueError) as exc:
        service.search(query="今天天气")

    assert "SERPER_API_KEY" in str(exc.value)


def test_guard_fires_before_any_network_call(monkeypatch):
    """守卫必须在**发起连接之前**生效。

    把 `HTTPSConnection` 换成会抛 AssertionError 的替身：若守卫失效（或位置被挪进
    `try` 里被吞掉），本用例会看到 AssertionError / 返回 {"error": True} 而不是
    ValueError，从而失败。
    """
    _clear_env_key(monkeypatch)

    def _no_connection(*args, **kwargs):
        raise AssertionError("缺密钥时不应发起任何网络连接")

    monkeypatch.setattr(http.client, "HTTPSConnection", _no_connection)

    with pytest.raises(ValueError):
        WebSearchService().search(query="x")


def test_search_with_blank_explicit_key_raises(monkeypatch):
    """显式传空串同样视为缺密钥 —— 不能因为「参数传了」就放行空 X-API-KEY。"""
    _clear_env_key(monkeypatch)

    with pytest.raises(ValueError):
        WebSearchService(api_key="").search(query="x")


def test_env_var_key_is_accepted(monkeypatch):
    """密钥来自环境变量是既有契约：配了就不能在守卫处误报。"""
    monkeypatch.setenv("SERPER_API_KEY", "env-key")

    assert WebSearchService().api_key == "env-key"


def test_explicit_key_is_accepted(monkeypatch):
    """显式密钥优先，且同样放行。"""
    _clear_env_key(monkeypatch)

    assert WebSearchService(api_key="explicit").api_key == "explicit"


def test_construction_does_not_require_key(monkeypatch):
    """构造期必须放行，否则 chat 路由共用的 get_services() 会连坐 500。

    这条断言把「守卫在 search() 而非 __init__」这个**设计决定**钉住，
    防止后人顺手挪回构造期 / 启动期（那会变成方案 B，改变可部署性）。
    """
    _clear_env_key(monkeypatch)

    WebSearchService()  # 不抛即通过
