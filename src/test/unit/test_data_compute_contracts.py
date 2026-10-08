from __future__ import annotations

import io
import tarfile
from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest

from src.agent.tools.compute import (
    DockerSandboxRunner,
    GenerateChartArgs,
    RenderTableArgs,
    SandboxExecuteArgs,
    SandboxLimits,
    SandboxSpec,
    StructuredData,
    _collect_outputs,
    _render_chart,
    _render_table,
)
from src.agent.tools.database_read import (
    DatabaseCredential,
    ExecuteReadonlySqlArgs,
    HostPortDatabaseEgressPolicy,
    PostgresDatabase,
    parse_readonly_sql,
)
from src.agent.tools.datasets import (
    ProfileDatasetAdapter,
    QueryDatasetArgs,
    _unique_sheet_frames,
    _validate_dataset_query,
)
from src.agent.config import Phase6Limits
from src.agent.tools.database_write import PostgresWriteTargetFactory
from src.agent.backends.persistence.models import DataSourceRow, DataSourceTablePolicyRow


class NeverCancelled:
    def is_cancelled(self) -> bool:
        return False


def policy() -> dict[tuple[str, str], DataSourceTablePolicyRow]:
    row = DataSourceTablePolicyRow(
        tenant_id="tenant",
        datasource_id="source",
        schema_name="sales",
        table_name="orders",
        readable_columns=["id", "amount"],
        writable_columns=[],
        allow_insert=False,
        allow_update=False,
        allow_delete=False,
        max_write_rows=0,
    )
    return {("sales", "orders"): row}


def test_phase6_fixed_limits_match_the_formal_contract() -> None:
    limits = Phase6Limits()
    assert (limits.readonly_sql_default_rows, limits.readonly_sql_max_rows) == (200, 1000)
    assert (
        limits.readonly_sql_default_timeout_ms,
        limits.readonly_sql_max_timeout_ms,
    ) == (10_000, 30_000)
    assert (
        limits.dataset_profile_default_columns,
        limits.dataset_profile_max_columns,
    ) == (50, 100)
    assert (
        limits.sandbox_cpus,
        limits.sandbox_memory_bytes,
        limits.sandbox_writable_disk_bytes,
        limits.sandbox_pids,
    ) == (2.0, 2 * 1024**3, 2 * 1024**3, 64)
    assert (limits.chart_recommended_points, limits.chart_max_points) == (5000, 20000)
    assert (limits.chart_max_series, limits.chart_max_categories) == (20, 200)
    assert (limits.table_default_rows, limits.table_max_rows) == (100, 500)
    assert (
        limits.table_max_columns,
        limits.table_cell_max_bytes,
        limits.table_serialized_max_bytes,
    ) == (30, 4096, 256 * 1024)
    with pytest.raises(ValueError, match="must not exceed"):
        Phase6Limits(readonly_sql_default_rows=1001)


def test_readonly_sql_denies_system_catalog_and_unsafe_functions() -> None:
    with pytest.raises(PermissionError, match="system catalog"):
        parse_readonly_sql("SELECT relname FROM pg_catalog.pg_class", policy())
    with pytest.raises(PermissionError, match="pg_read_file"):
        parse_readonly_sql(
            "SELECT pg_read_file('/etc/passwd') FROM sales.orders", policy()
        )
    with pytest.raises(ValueError, match="read-only"):
        parse_readonly_sql("CREATE TABLE sales.bad(id int)", policy())


def test_phase6_argument_defaults_and_hard_bounds() -> None:
    readonly = ExecuteReadonlySqlArgs(sql="SELECT id FROM sales.orders")
    assert (readonly.row_limit, readonly.timeout_ms) == (200, 10_000)
    ExecuteReadonlySqlArgs(
        sql="SELECT id FROM sales.orders", row_limit=1000, timeout_ms=30_000
    )
    with pytest.raises(ValueError):
        ExecuteReadonlySqlArgs(sql="SELECT id FROM sales.orders", row_limit=1001)
    with pytest.raises(ValueError):
        ExecuteReadonlySqlArgs(sql="SELECT id FROM sales.orders", timeout_ms=30_001)

    query = QueryDatasetArgs(dataset_ref="dataset", query="SELECT * FROM dataset")
    assert query.row_limit == 100
    with pytest.raises(ValueError):
        QueryDatasetArgs(
            dataset_ref="dataset", query="SELECT * FROM dataset", row_limit=1001
        )

    sandbox = SandboxExecuteArgs(
        analysis_id="analysis",
        code="print('ok')",
        inputs=[{"name": "input.json", "evidence_id": "evidence"}],
    )
    assert sandbox.timeout_ms == 120_000
    with pytest.raises(ValueError):
        SandboxExecuteArgs(
            analysis_id="analysis",
            code="print('ok')",
            inputs=[{"name": "input.json", "evidence_id": "evidence"}],
            timeout_ms=300_001,
        )

    table = RenderTableArgs(
        inline=StructuredData(columns=["value"], rows=[[1]]),
        evidence_ids=["evidence"],
    )
    assert table.max_rows == 100
    with pytest.raises(ValueError):
        RenderTableArgs(
            inline=StructuredData(columns=["value"], rows=[[1]]),
            evidence_ids=["evidence"],
            max_rows=501,
        )


def test_postgres_metadata_and_query_transactions_are_readonly() -> None:
    class Cursor:
        def __init__(self) -> None:
            self.calls = []

        def execute(self, statement, parameters=None) -> None:
            self.calls.append((statement, parameters))

    database = object.__new__(PostgresDatabase)
    database.limits = Phase6Limits()
    cursor = Cursor()
    database._configure_readonly(cursor)
    assert cursor.calls == [
        ("SET TRANSACTION READ ONLY", None),
        ("SELECT set_config('statement_timeout', %s, true)", ("10000",)),
    ]


def test_dataset_profile_defaults_to_50_and_rejects_more_than_100_columns() -> None:
    class Datasets:
        def table(self, tenant_id, task_id, dataset_ref, table):
            del tenant_id, task_id, dataset_ref, table
            frame = pd.DataFrame({f"c{index}": [index] for index in range(101)})
            return {}, frame

    context = type("Context", (), {"tenant_id": "tenant", "task_id": "task"})()
    adapter = ProfileDatasetAdapter(Datasets())
    default = adapter.execute(context, {"dataset_ref": "dataset"})
    assert len(default.data["columns_profiled"]) == 50
    assert len(default.data["columns_skipped"]) == 51
    assert default.truncated is True
    accepted = adapter.execute(
        context,
        {"dataset_ref": "dataset", "columns": [f"c{index}" for index in range(100)]},
    )
    assert accepted.ok
    denied = adapter.execute(
        context,
        {"dataset_ref": "dataset", "columns": [f"c{index}" for index in range(101)]},
    )
    assert denied.error.code == "invalid_argument"


def test_dataset_query_denies_external_and_introspection_functions() -> None:
    for query in (
        "SELECT * FROM read_json_auto('https://example.test/data.json')",
        "SELECT * FROM duckdb_tables()",
        "SELECT * FROM glob('/tmp/*')",
    ):
        with pytest.raises(PermissionError):
            _validate_dataset_query(query)


def test_database_egress_policy_is_exact_host_and_port() -> None:
    row = DataSourceRow(
        datasource_id="source",
        tenant_id="tenant",
        name="source",
        host="db.example",
        port=5432,
        database_name="business",
        ssl_mode="require",
        credential_ref="secret",
        enabled=True,
        updated_at=datetime.now(timezone.utc),
    )
    allowed = HostPortDatabaseEgressPolicy(("db.example:5432",))
    allowed.validate(row)
    with pytest.raises(PermissionError, match="egress policy"):
        HostPortDatabaseEgressPolicy(("other.example:5432",)).validate(row)


def test_write_pool_is_separate_and_retires_on_datasource_revision() -> None:
    now = datetime.now(timezone.utc)
    row = DataSourceRow(
        datasource_id="source",
        tenant_id="tenant",
        name="source",
        host="db.example",
        port=5432,
        database_name="business",
        ssl_mode="require",
        credential_ref="secret",
        enabled=True,
        updated_at=now,
    )
    factory = PostgresWriteTargetFactory()
    credential = DatabaseCredential("writer", "secret")
    first = factory.create(row, credential)
    assert factory.create(row, credential) is first
    row.updated_at = now + timedelta(seconds=1)
    replacement = factory.create(row, credential)
    assert replacement is not first
    assert first.closed is True


def test_xlsx_safe_sheet_names_remain_unique() -> None:
    frames = _unique_sheet_frames(
        {
            "A-B": pd.DataFrame({"value": [1]}),
            "A B": pd.DataFrame({"value": [2]}),
        }
    )
    assert list(frames) == ["A_B", "A_B_2"]


def test_table_header_truncation_is_reported() -> None:
    args = RenderTableArgs(
        inline=StructuredData(columns=["x" * 5000], rows=[[1]]),
        evidence_ids=["evidence"],
    )
    payload, rows, truncated = _render_table(args, args.inline)
    assert rows == 1
    assert truncated is True
    assert b"[TRUNCATED]" in payload


def test_chart_and_table_secondary_hard_limits() -> None:
    category_args = GenerateChartArgs(
        chart_type="line",
        data=StructuredData(
            columns=["x", "y"], rows=[[index, index] for index in range(201)]
        ),
        evidence_ids=["evidence"],
        x="x",
        y="y",
    )
    with pytest.raises(ValueError, match="200 category"):
        _render_chart(category_args, category_args.data)

    series_args = GenerateChartArgs(
        chart_type="line",
        data=StructuredData(
            columns=["x", "y", "series"],
            rows=[[0, index, f"s{index}"] for index in range(21)],
        ),
        evidence_ids=["evidence"],
        x="x",
        y="y",
        series="series",
    )
    with pytest.raises(ValueError, match="20 series"):
        _render_chart(series_args, series_args.data)

    table_args = RenderTableArgs(
        inline=StructuredData(
            columns=[f"c{index}" for index in range(31)],
            rows=[["x" * 1000 for _ in range(31)] for _ in range(50)],
        ),
        evidence_ids=["evidence"],
        max_rows=50,
    )
    payload, rendered, truncated = _render_table(table_args, table_args.inline)
    assert rendered < 50
    assert truncated is True
    assert len(payload) <= 256 * 1024


def test_sandbox_total_output_limit_is_enforced(tmp_path) -> None:
    output = tmp_path / "outputs"
    output.mkdir()
    (output / "first.bin").write_bytes(b"123")
    (output / "second.bin").write_bytes(b"456")
    with pytest.raises(ValueError, match="total output limit"):
        _collect_outputs(
            output,
            SandboxLimits(single_output_bytes=4, total_output_bytes=5),
        )


def test_docker_sandbox_command_enforces_limits_without_secrets(
    tmp_path, monkeypatch
) -> None:
    captured: dict[str, object] = {}

    class Process:
        stdout = io.BytesIO(b"12345")
        stderr = io.BytesIO()
        returncode = None

        def poll(self):
            return self.returncode

        def wait(self, timeout=None):
            self.returncode = 137
            return self.returncode

        def kill(self):
            return None

    archive_bytes = io.BytesIO()
    with tarfile.open(fileobj=archive_bytes, mode="w") as archive:
        info = tarfile.TarInfo("./result.txt")
        info.size = 2
        archive.addfile(info, io.BytesIO(b"ok"))

    class ArchiveProcess:
        stdout = io.BytesIO(archive_bytes.getvalue())
        stderr = io.BytesIO()
        returncode = 0

        def wait(self, timeout=None):
            return self.returncode

        def kill(self):
            return None

    def popen(command, **kwargs):
        captured.setdefault("popen_commands", []).append(command)
        if command[1] == "exec":
            return ArchiveProcess()
        captured["command"] = command
        captured["env"] = kwargs["env"]
        return Process()

    class Completed:
        def __init__(self, command):
            self.returncode = 0
            self.stdout = "0" if command[1] == "exec" else ""
            self.stderr = ""

    def run(command, **kwargs):
        captured.setdefault("lifecycle", []).append(command)
        return Completed(command)

    monkeypatch.setattr("src.agent.tools.compute.shutil.which", lambda _: "docker")
    monkeypatch.setattr("src.agent.tools.compute.subprocess.Popen", popen)
    monkeypatch.setattr("src.agent.tools.compute.subprocess.run", run)
    code = tmp_path / "code.py"
    inputs = tmp_path / "inputs"
    outputs = tmp_path / "outputs"
    code.write_text("print('ok')", encoding="utf-8")
    inputs.mkdir()
    outputs.mkdir()
    runner = DockerSandboxRunner(limits=SandboxLimits(log_bytes=4))

    result = runner.run(
        SandboxSpec(
            tool_call_id="call",
            code_path=code,
            inputs_dir=inputs,
            outputs_dir=outputs,
            timeout_ms=1000,
            cancellation_token=NeverCancelled(),
        )
    )

    command = captured["command"]
    assert result.exit_code == 0
    assert result.stdout == "1234"
    assert result.truncated_output is True
    assert command[command.index("--network") + 1] == "none"
    assert command[command.index("--runtime") + 1] == "runsc"
    assert command[command.index("--cpus") + 1] == "2.0"
    assert command[command.index("--memory") + 1] == str(2 * 1024**3)
    assert command[command.index("--pids-limit") + 1] == "64"
    assert "--read-only" in command and "ALL" in command
    assert not any("dst=/outputs" in item for item in command)
    assert any(item.startswith("/outputs:rw,noexec") for item in command)
    assert any(item[1] == "exec" and item[3] == "tar" for item in captured["popen_commands"])
    assert any(item[1] == "kill" for item in captured["lifecycle"])
    assert captured["lifecycle"][-1][1:3] == ["rm", "--force"]
    assert captured["env"].keys() == {"PATH"}


def test_external_ledger_ddl_matches_write_adapter_contract() -> None:
    ddl = (
        __import__("pathlib").Path("ops/external_postgres/agent_write_idempotency.sql")
        .read_text(encoding="utf-8")
        .lower()
    )
    for column in (
        "operation_key",
        "write_id",
        "plan_hash",
        "status",
        "affected_rows",
        "result_hash",
        "result_json",
        "committed_at",
    ):
        assert column in ddl
