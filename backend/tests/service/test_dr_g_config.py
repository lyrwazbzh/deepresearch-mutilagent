"""T01 回归测试：dr_g 的密钥读取不得回退到硬编码值。

背景：``app/service/dr_g.py`` 曾以 ``os.getenv(NAME, "<真实密钥>")`` 的形式携带真实
API 密钥作为兜底值，并随源码进入公开仓库。本测试锁定三件事：

1. 环境变量缺失时显式失败，不再静默回退；
2. 模块不再导出携带密钥的常量；
3. ``websearch`` 自行拼接 ``Bearer `` 前缀（环境变量中存放裸密钥）。

全部用例不依赖基础设施，可直接在 CI 中运行。
"""

import pathlib
import re

import pytest

import service.dr_g as dr_g
from service.dr_g import get_llm_api_key, get_search_api_key


KEY_VARS = [
    ("BOCHA_API_KEY", get_search_api_key),
    ("DASHSCOPE_API_KEY", get_llm_api_key),
]


@pytest.mark.parametrize("var,getter", KEY_VARS)
def test_missing_env_raises_runtime_error(monkeypatch, var, getter):
    """环境变量缺失时必须抛 RuntimeError，且错误信息包含变量名。"""
    monkeypatch.delenv(var, raising=False)

    with pytest.raises(RuntimeError) as excinfo:
        getter()

    assert var in str(excinfo.value), "错误信息应指出缺失的变量名，便于定位"


@pytest.mark.parametrize("var,getter", KEY_VARS)
def test_empty_env_raises_runtime_error(monkeypatch, var, getter):
    """空字符串等同缺失，不得被当作有效密钥。"""
    monkeypatch.setenv(var, "")

    with pytest.raises(RuntimeError):
        getter()


@pytest.mark.parametrize("var,getter", KEY_VARS)
def test_returns_configured_value(monkeypatch, var, getter):
    """配置后应原样返回环境变量值（不做前缀加工）。"""
    monkeypatch.setenv(var, "test-key-not-real")

    assert getter() == "test-key-not-real"


def test_legacy_key_constants_are_gone():
    """历史实现以模块常量形式携带真实密钥，必须彻底移除。"""
    assert not hasattr(dr_g, "SEARCH_API_KEY")
    assert not hasattr(dr_g, "LLM_API_KEY")


def test_module_source_contains_no_key_literals():
    """源码中不得再出现密钥样式的字面量。"""
    source = pathlib.Path(dr_g.__file__).read_text(encoding="utf-8")

    assert not re.search(r"sk-[A-Za-z0-9]{16,}", source)
    assert not re.search(r"ragflow-[A-Za-z0-9]{10,}", source)


def test_websearch_prefixes_bearer(monkeypatch):
    """websearch 必须在调用处拼接 Bearer 前缀 —— 环境变量中存放的是裸密钥。

    历史实现把 "Bearer " 写进了常量默认值，导致从环境变量读取时缺少前缀，
    请求会因鉴权格式错误而失败。
    """
    monkeypatch.setenv("BOCHA_API_KEY", "test-key-not-real")
    captured = {}

    class _FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {"data": {"webPages": {"value": []}}}

    def _fake_post(url, headers=None, data=None, timeout=None):
        captured["headers"] = headers
        captured["url"] = url
        return _FakeResponse()

    monkeypatch.setattr(dr_g.requests, "post", _fake_post)

    dr_g.websearch("测试查询")

    assert captured["headers"]["Authorization"] == "Bearer test-key-not-real"
