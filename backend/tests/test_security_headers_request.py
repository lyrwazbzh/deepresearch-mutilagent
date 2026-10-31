"""T46 回归测试：CSP 中间件的**请求级**行为。

背景（§4 总门禁 finding #10）：``tests/core/test_security_headers.py`` 对「中间件是否接线」
的唯一自动化锁是 ``app_main.py`` 的**源码文本**断言 —— 把中间件注册包进一个恒假分支，
那个断言仍然会绿。本文件用 ``TestClient`` 发**真实请求**，把「响应头真的被加上」也锁上。

## 为什么在子进程里跑

``app_main`` 一被导入就会连带拉起全部路由、模型与数据库引擎；本仓的 ``tests/conftest.py``
为把单测压到秒级，把 ``service`` / ``models`` 等包替换成了**不执行 __init__ 的占位包**，
并在缺名时逐个人工补桩 —— 这是 ``core/security_headers.py`` 里明确写下的设计取舍
（「其中的策略无法在无基础设施的测试里验证」）。

在同一个 pytest 会话里导入 ``app_main`` 会一路踩坑：占位包缺名（ResearchService →
deep_research_v2 重链 → models 顶层类）、以及**重复导入 models 触发
``Table '...' is already defined``**。改用**子进程**则每次都是干净解释器：
真实模块按真实顺序导入、没有会话残留，既不需要往本文件堆一长串桩，也不会污染其它测试。

代价：导入 ``app_main`` 实测约 16–18s（冷启动曾见 30s），本文件是全仓最重的一个测试（相对全量 ~25s 是明显增量）。
换来的是**唯一一处**对「中间件真的挂在 HTTP 响应上」的自动化锁，值这个价。

无基础设施：``TestClient`` 只在 ``with`` 块里触发 lifespan，探针**不使用**上下文管理器，
因此不会拉起 scheduler / DB 初始化。
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from core.security_headers import CONTENT_SECURITY_POLICY

BACKEND_DIR = Path(__file__).resolve().parents[1]
APP_DIR = BACKEND_DIR / "app"

# 在干净解释器里发四个真实请求，把「状态码 + CSP 头」原样吐给断言层。
# 注意：不加 `with` —— TestClient 只在上下文管理器内触发 lifespan。
_PROBE = """
import json

from starlette.testclient import TestClient

import app_main

client = TestClient(app_main.app)
result = {}
for path in ("/hello", "/openapi.json", "/docs", "/docsx"):
    resp = client.get(path)
    result[path] = {
        "status": resp.status_code,
        "csp": resp.headers.get("content-security-policy"),
    }
print("T46_RESULT=" + json.dumps(result))
"""


@pytest.fixture(scope="module")
def responses():
    env = dict(os.environ)
    env.setdefault("ENV", "test")
    # 与 tests/conftest.py 一致的测试占位值；子进程环境不会继承 pytest 进程内的 setdefault。
    env.setdefault(
        "JWT_SECRET_KEY", "test-only-jwt-secret-key-do-not-use-in-production-0123456789"
    )
    env.setdefault("POSTGRES_PASSWORD", "test-only-postgres-password")
    env["PYTHONPATH"] = str(APP_DIR)

    proc = subprocess.run(
        [sys.executable, "-c", _PROBE],
        cwd=str(BACKEND_DIR),
        env=env,
        capture_output=True,
        text=True,
        # 实测导入 16–18s（冷启动 30s）；180s 留 ~6 倍余量，挂死时也不至白等 10 分钟
        timeout=180,
    )
    assert proc.returncode == 0, f"探针进程失败：\n{proc.stderr[-3000:]}"

    payload = next(
        (line for line in proc.stdout.splitlines() if line.startswith("T46_RESULT=")),
        None,
    )
    assert payload, f"探针未输出结果：\n{proc.stdout[-3000:]}"
    return json.loads(payload.split("=", 1)[1])


def test_api_response_carries_csp(responses):
    """普通 API 响应必须带 CSP。删掉中间件注册时本用例必须失败（变异检查的靶子）。"""
    assert responses["/hello"]["status"] == 200
    assert responses["/hello"]["csp"] == CONTENT_SECURITY_POLICY


def test_openapi_json_is_exempt(responses):
    """Swagger UI 需要读取 openapi.json，严格 CSP 会打坏它，故整体豁免。"""
    assert responses["/openapi.json"]["status"] == 200
    assert responses["/openapi.json"]["csp"] is None


def test_docs_path_is_exempt(responses):
    assert responses["/docs"]["status"] == 200
    assert responses["/docs"]["csp"] is None


def test_prefix_sharing_path_is_not_exempt(responses):
    """/docsx 只是前缀相同，不得豁免 —— 这是 T37 路径段边界的核心。

    404 响应同样要经过中间件，所以这里顺带锁住「错误响应也不漏 CSP」。
    """
    assert responses["/docsx"]["status"] == 404
    assert responses["/docsx"]["csp"] == CONTENT_SECURITY_POLICY
