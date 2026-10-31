# Copyright © 2026 深圳市深维智见教育科技有限公司 版权所有
# 未经授权，禁止转售或仿制。

"""
T09 回归测试：text2sql SQL 校验封堵 UNION 绕过。

背景（事实 F-09）：旧校验用黑名单，FORBIDDEN 含 'UNION ALL SELECT' 但 UNION
本身在 ALLOWED 中，`SELECT ... UNION SELECT ...` 可绕过校验。

修复口径（ticket T09）：
- 主判据改为允许列表：语句必须以 SELECT 或 WITH 开头，且禁止任何形式的 UNION；
- 黑名单降为辅助兜底；
- 清理 '--'（有专门注释检查）与 'UNION ALL SELECT'（被 UNION 全禁覆盖）的重复项。

`validate_sql` 是纯函数（只依赖类常量），不依赖数据库 / LLM。

T39 补充（2026-09-13）：票面矩阵点名的 TRUNCATE / ALTER / 子查询 / 超长 SQL / 大小写混写
几项原文件未覆盖，已在下方补齐。T39 票面写「新增 backend/tests/service/test_text2sql_validate.py」，
但该文件在 T09 时已建立，故本票实为**扩展既有文件**而非新建。
"""

import pytest
from service.text2sql_service import Text2SQLService


@pytest.fixture(scope="module")
def service() -> Text2SQLService:
    """不传 db_connection_string，不建 engine、不触网。"""
    return Text2SQLService(
        llm_api_key="test-only-key",
        llm_base_url="http://localhost:9999/v1",
    )


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT 1 UNION SELECT 2",
        "SELECT 1 UNION ALL SELECT 2",
        "select 1 union select 2",                       # 小写绕过
        "SELECT 1 uNiOn SeLeCt 2",                       # 混合大小写绕过
        "SELECT 1\nUNION\nSELECT 2",                     # 换行分隔绕过
        "SELECT metric_name FROM industry_stats UNION SELECT policy_name FROM policy_data",
        "WITH t AS (SELECT 1) SELECT * FROM t UNION SELECT 2",
    ],
)
def test_union_variants_are_rejected(service, sql):
    ok, msg = service.validate_sql(sql)
    assert not ok, f"未拦截 UNION 变体: {sql!r}"
    assert msg


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM t; DROP TABLE t",
        "SELECT 1; SELECT 2",
        "DELETE FROM t",
        "UPDATE t SET a = 1",
        "INSERT INTO t VALUES (1)",
        "DROP TABLE t",
        "SELECT 1 -- comment",
        "SELECT 1 /* comment */",
        "WITH x AS (DELETE FROM t RETURNING *) SELECT * FROM x",  # PG 数据修改 CTE
        "",
        "   ",
        None,
        "EXEC sp_help",
        # T39 补齐：票面矩阵点名的 DDL / 高危 DML（实际由「必须以 SELECT 或 WITH 开头」拦截）
        "TRUNCATE TABLE t",
        "ALTER TABLE t ADD COLUMN c int",
        "CREATE TABLE t (a int)",
        "GRANT ALL ON t TO someone",
        "REVOKE ALL ON t FROM someone",
        # FORBIDDEN_KEYWORDS 兜底族代表：时间盲注
        "SELECT pg_sleep(10)",
    ],
)
def test_dangerous_sql_is_rejected(service, sql):
    ok, msg = service.validate_sql(sql)
    assert not ok, f"未拦截危险输入: {sql!r}"
    assert msg


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT id, name FROM users WHERE id = 1",
        "select metric_name, metric_value from industry_stats where year = 2024",
        "SELECT industry_name, SUM(metric_value) AS total FROM industry_stats GROUP BY industry_name ORDER BY total DESC LIMIT 10",
        "SELECT 1;",                                      # 允许末尾分号
        "WITH t AS (SELECT 1 AS v) SELECT v FROM t",      # WITH 开头的 CTE 查询
        "SELECT a FROM t INNER JOIN b ON t.id = b.id WHERE a LIKE '%x%'",
        "SELECT CASE WHEN a > 1 THEN 'high' ELSE 'low' END FROM t",
        # 词边界回归：含 union 字样的标识符不应被误拦（第 3 批审查 finding）
        "SELECT union_id FROM t WHERE reunion_tag = 'x'",
        "SELECT * FROM trade_union ORDER BY id",
        # T39 补齐：票面矩阵点名的「子查询」与「大小写混写」
        "SELECT * FROM (SELECT id, name FROM users WHERE id > 1) AS sub",
        "SELECT a FROM t WHERE a IN (SELECT b FROM u)",
        "SELECT a, (SELECT MAX(b) FROM u WHERE u.a = t.a) AS mx FROM t",
        "SeLeCt A fRoM t WhErE a = 1",
    ],
)
def test_legitimate_readonly_sql_passes(service, sql):
    ok, msg = service.validate_sql(sql)
    assert ok, f"误拦合法只读查询: {sql!r}（原因: {msg}）"


def test_real_union_is_still_blocked(service):
    """词边界收紧后，真正的 UNION 语句必须仍然被拦截。"""
    for sql in ("SELECT 1 UNION SELECT 2", "SELECT 1 UNION ALL SELECT 2"):
        ok, msg = service.validate_sql(sql)
        assert not ok, f"未拦截: {sql!r}"
        assert msg


def test_very_long_select_is_accepted(service):
    """T39 边界：超长只读 SQL 不应被误伤。

    本校验器不设长度上限（长度约束由上层提示词承担），因此 800 列的长语句必须放行；
    若将来加入长度限制，本用例会失败并提醒同步更新票面口径。
    """
    sql = "SELECT " + ", ".join(f"c{i}" for i in range(800)) + " FROM t WHERE a = 1"

    assert len(sql) > 4000
    ok, msg = service.validate_sql(sql)
    assert ok, f"误拦超长只读查询（原因: {msg}）"
