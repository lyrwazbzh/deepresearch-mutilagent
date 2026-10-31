"""T72 回归：Bocha 搜索的 `Authorization` 必须带 `Bearer ` 前缀。

## 缺陷

`tool_executor.py` 的 `_websearch_sync` 直接写 `'Authorization': self.search_api_key`
—— **缺 `Bearer ` 前缀**。同一端点（`api.bochaai.com/v1/web-search` / `api.bocha.cn/...`）
的另外 3 处调用（`dr_g.py`、`news_collection_service.py`、`scout.py`）**全部**带前缀，
`bidding_service.py` 的 `APPCODE ` 是另一家厂商、不参与本条。

## 为什么值得一个用例

它**不是**「报错很响」的那类缺陷：`_websearch_sync` 用 `except Exception` 兜底并 `return []`
（`tool_executor.py:222-224`），所以缺前缀换来的 401 会被静默吞掉，表现为
「V1 研究路线的联网检索**永远搜不到东西**」—— 功能静默失效，最难在运行期发现的那种。

## 判据

1. **行为层** —— 拦下 `requests.post`，直接断言真的发出去的 `Authorization` 值；
2. **结构层（目录级）** —— `app/service/**/*.py` 里每一处 `Authorization` 头都必须是
   **带厂商前缀的 f-string**（`Bearer ` 或 `APPCODE `），杜绝「裸变量」形态复发。
   作用域限 `app/service/`（缺陷所在层），不越界到 router / core，避免过度约束 ——
   与 T56 目录级锁「作用域取向」一致。
"""

import importlib
import re
from pathlib import Path

import pytest

te = importlib.import_module("service.tool_executor")

SERVICE_DIR = Path(te.__file__).parent
TEST_KEY = "sk-t72-search-key-not-a-real-credential"


class _FakeResponse:
    """最小响应替身：只提供 `_websearch_sync` 真正用到的两个方法。"""

    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


@pytest.fixture
def executor():
    return te.ToolExecutor(
        search_api_key=TEST_KEY,
        llm_api_key="sk-not-used",
        llm_base_url="http://localhost:1/v1",
    )


def test_bocha_request_sends_bearer_prefixed_authorization(monkeypatch, executor):
    """核心断言：发往 Bocha 的请求头必须是 `Bearer <key>`，而不是裸 key。"""
    captured = {}

    def _fake_post(url, headers=None, data=None, timeout=None):
        captured["url"] = url
        captured["headers"] = dict(headers or {})
        return _FakeResponse({"data": {"webPages": {"value": []}}})

    monkeypatch.setattr(te.requests, "post", _fake_post)

    executor._websearch_sync("测试查询", 5)

    assert captured.get("url"), "没有发出请求，用例失去观测点"
    assert "bocha" in captured["url"], f"请求打到了非预期端点：{captured['url']}"

    authorization = captured["headers"].get("Authorization")
    assert authorization, f"请求头里没有 Authorization：{captured['headers']}"
    assert authorization == f"Bearer {TEST_KEY}", (
        f"Authorization 头是 {authorization!r} —— 缺 `Bearer ` 前缀会让 Bocha 返回 401，"
        "而 `_websearch_sync` 的兜底会把它吞成「搜索无结果」（T72 / §4 补审）"
    )


def test_search_failure_is_swallowed_into_empty_result(monkeypatch, executor):
    """记录**为什么**这条缺陷此前不可见：失败被吞成空列表，不会向调用方报错。

    这条用例不是为「吞异常」背书，而是把该行为钉在明处 —— 若将来改成 fail-loud，
    这里会红，提醒同步更新上面那条断言的措辞与严重性描述。
    """

    def _boom(*_args, **_kwargs):
        raise RuntimeError("HTTP 401 Unauthorized")

    monkeypatch.setattr(te.requests, "post", _boom)

    assert executor._websearch_sync("测试查询", 5) == []


# --------------------------------------------------------------------------- 结构层（目录级）

_AUTH_HEADER_RE = re.compile(
    r"""['"]Authorization['"]\s*:\s*(?P<value>[^,\n]+)""",
)
_PREFIXED_RE = re.compile(r"""f['"][^'"]*(?:Bearer |APPCODE )""")


def test_every_authorization_header_carries_a_vendor_prefix():
    """🔒 **目录级**锁：`app/service/**/*.py` 里不得再出现「裸 key」形态的 Authorization 头。

    判据是**形状**而非具体厂商：值必须是字面量 f-string 且含 `Bearer ` 或 `APPCODE ` 前缀。
    新增厂商时这里会红一次，逼作者显式把新前缀加进判据 —— 正是「缺陷发生过一次，
    就不该以同一形状发生第二次」的落地方式。
    """
    offenders = []
    scanned = 0

    for path in sorted(SERVICE_DIR.rglob("*.py")):
        src = path.read_text(encoding="utf-8")
        for match in _AUTH_HEADER_RE.finditer(src):
            scanned += 1
            value = match.group("value").strip().rstrip(",").strip()
            if not _PREFIXED_RE.search(value):
                line = src.count("\n", 0, match.start()) + 1
                offenders.append(f"{path.relative_to(SERVICE_DIR)}:{line} -> {value}")

    assert not offenders, (
        f"以下 Authorization 头没有厂商前缀（裸 key）：{offenders}。"
        "Bocha 需 `f'Bearer {…}'`、阿里 APPCODE 需 `f'APPCODE {…}'` —— "
        "裸值会被判 401，并被各处的兜底静默吞成「无结果」"
    )

    # 反恒真：判据被改坏 / 扫描范围收缩时，这里先红，而不是让锁静默退化
    assert scanned >= 5, (
        f"本锁只扫到 {scanned} 处 Authorization 头（预期 ≥5）—— "
        "判据或目录范围可能已变，请复核是否仍有判别力"
    )
