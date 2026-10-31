"""T10 回归测试：text2sql 使用只读连接串（解析优先级 + 路由接线）。

背景（事实 F-10）：`text2sql_service.execute_sql` 把模型生成的 SQL 直接交给
`text(sql)` 执行。T09 的校验是**解析式防御**，任何一处漏判都会直接作用在主库上；
唯一根治手段是让 text2sql 用一个只有 SELECT 权限的数据库账号
（`CREATE ROLE text2sql_ro ...` + `GRANT SELECT ON ALL TABLES ...`）。

本测试**不连数据库**：只验证 (1) 连接串解析优先级，(2) 路由确实走这条解析。
"""

import pathlib

import core.database as database

BACKEND_DIR = pathlib.Path(__file__).resolve().parents[2]
ROUTER_SOURCE = (BACKEND_DIR / "app" / "router" / "database_router.py").read_text(
    encoding="utf-8"
)

READONLY_URL = "postgresql://text2sql_ro:pw@db:5432/industry_assistant"


def test_prefers_readonly_url_when_configured(monkeypatch):
    """配了只读连接串就必须用它 —— 这是本票的全部意义所在。"""
    monkeypatch.setenv("TEXT2SQL_DATABASE_URL", READONLY_URL)

    assert database.resolve_text2sql_url() == READONLY_URL


def test_falls_back_to_main_url_when_absent(monkeypatch):
    """未配置时行为与本票之前一致（回退主库），避免静默变成「连不上」。"""
    monkeypatch.delenv("TEXT2SQL_DATABASE_URL", raising=False)

    assert database.resolve_text2sql_url() == database.DATABASE_URL


def test_empty_value_is_treated_as_unset(monkeypatch):
    """`.env` 里留空（`TEXT2SQL_DATABASE_URL=`）必须回退主库，而不是造出空连接串。"""
    monkeypatch.setenv("TEXT2SQL_DATABASE_URL", "")

    assert database.resolve_text2sql_url() == database.DATABASE_URL


def test_router_uses_the_resolver():
    """接线断言：路由必须调用 `resolve_text2sql_url`，且不得再自行拼第二套连接串。

    有意保留源码断言：本项验收目标就是「路由不再有第二套连接串构造」这一**结构**事实
    （与 T07 的 `test_database_module_no_longer_defaults_the_password` 同型）；
    真正的行为断言在 `resolve_text2sql_url` 上，已由上面三个用例覆盖。
    """
    assert "resolve_text2sql_url()" in ROUTER_SOURCE
    assert 'os.getenv("DATABASE_URL"' not in ROUTER_SOURCE
    assert 'os.getenv("POSTGRES_PASSWORD"' not in ROUTER_SOURCE
