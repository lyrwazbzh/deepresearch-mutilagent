"""T02 回归测试：JWT 密钥必须显式配置，且拒绝弱值。

背景：``app/core/security.py`` 曾以
``SECRET_KEY = os.getenv("JWT_SECRET_KEY", "<公开字符串>")`` 的形式提供默认密钥。
任何未覆盖该变量的部署，都能被攻击者用公开可知的密钥伪造合法 Token。
本测试锁定三件事：

1. 变量缺失 / 为空 → **导入即失败**，不得静默回退到默认值；
2. 长度不足、或沿用历史默认值 → 同样拒绝；
3. 合规的强随机值 → 正常导入，且 ``SECRET_KEY`` 与环境变量一致。

校验发生在 `security.py` 的**导入期**，因此这里用 ``importlib`` 以独立模块名重复执行
源文件，让每次校验都真实跑一遍（而不是命中 ``sys.modules`` 缓存）。

全部用例不依赖基础设施，可直接在 CI 中运行。
"""

import importlib.util
import pathlib
import re
import secrets

import pytest

SECURITY_PATH = (
    pathlib.Path(__file__).resolve().parents[2] / "app" / "core" / "security.py"
)

# 历史实现写死在源码里的默认密钥。它有 43 个字符，仅靠长度校验拦不住，必须单独拒绝。
LEGACY_DEFAULT_KEY = "your-super-secret-key-change-in-production"

_load_seq = 0


def _exec_security_module():
    """以独立模块对象重新执行 ``security.py`` 并返回模块。

    导入期的校验若失败，异常会原样抛出 —— 这正是本测试要断言的「启动即失败」。
    """
    global _load_seq
    _load_seq += 1
    spec = importlib.util.spec_from_file_location(
        f"_security_under_test_{_load_seq}", SECURITY_PATH
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(autouse=True)
def _clear_secret_key(monkeypatch):
    """默认清空密钥，让每个用例显式声明自己要验证的取值。"""
    monkeypatch.delenv("JWT_SECRET_KEY", raising=False)


def test_missing_secret_key_fails_at_import():
    """未配置密钥时，导入模块即应失败，并在信息中指明变量名。"""
    with pytest.raises(RuntimeError) as excinfo:
        _exec_security_module()

    assert "JWT_SECRET_KEY" in str(excinfo.value)


def test_empty_secret_key_fails_at_import(monkeypatch):
    """空字符串等同缺失，不得被当作有效密钥。"""
    monkeypatch.setenv("JWT_SECRET_KEY", "")

    with pytest.raises(RuntimeError) as excinfo:
        _exec_security_module()

    assert "JWT_SECRET_KEY" in str(excinfo.value)


def test_short_secret_key_is_rejected(monkeypatch):
    """长度不足的弱值必须被拒绝。"""
    monkeypatch.setenv("JWT_SECRET_KEY", "short")

    with pytest.raises(RuntimeError) as excinfo:
        _exec_security_module()

    message = str(excinfo.value)
    assert "长度" in message or "弱" in message


def test_legacy_default_key_is_rejected(monkeypatch):
    """历史默认值足够长，仅靠长度校验拦不住，必须被显式列入拒绝名单。"""
    monkeypatch.setenv("JWT_SECRET_KEY", LEGACY_DEFAULT_KEY)

    with pytest.raises(RuntimeError):
        _exec_security_module()


def test_strong_secret_key_is_accepted(monkeypatch):
    """合规的强随机值应正常导入，且 SECRET_KEY 与环境变量完全一致。"""
    strong = secrets.token_urlsafe(48)
    monkeypatch.setenv("JWT_SECRET_KEY", strong)

    module = _exec_security_module()

    assert module.SECRET_KEY == strong


def test_no_getenv_default_for_secret_key():
    """密钥不得再通过 ``os.getenv("JWT_SECRET_KEY", <默认值>)`` 的形式提供兜底。"""
    source = SECURITY_PATH.read_text(encoding="utf-8")

    assert not re.search(r'getenv\(\s*["\']JWT_SECRET_KEY["\']\s*,', source)
