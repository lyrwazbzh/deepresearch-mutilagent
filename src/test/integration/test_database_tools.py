from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from src.agent.tools.database_read import (
    ColumnMetadata,
    DataSourceService,
    DatabaseCredential,
    ExecuteReadonlySqlAdapter,
    InspectSchemaAdapter,
    SampleTableAdapter,
    TableMetadata,
    VersionedPostgresFactory,
    authorize_lineage,
    parse_readonly_sql,
)
from src.agent.backends.persistence.models import (
    DataSourceRow,
    DataSourceTablePolicyRow,
    DataSourceUserGrantRow,
)
from src.agent.runtime.tool_runtime import ToolExecutionContext
from src.agent.task_service import CreateTaskRequest


class Token:
    def is_cancelled(self):
        return False


class Credentials:
    def __init__(self):
        self.calls = []

    def resolve(self, credential_ref, mode):
        self.calls.append((credential_ref, mode))
        return DatabaseCredential("reader", "secret")


class Database:
    def __init__(self):
        self.samples = []
        self.queries = []

    def list_tables(self):
        return [("private", "secret"), ("sales", "orders")]

    def describe(self, schema, table):
        return TableMetadata(
            columns=[
                ColumnMetadata(schema, table, "id", "integer", False),
                ColumnMetadata(schema, table, "amount", "numeric", False),
                ColumnMetadata(schema, table, "secret", "text", True),
            ],
            primary_key=["id"],
            foreign_keys=[],
        )

    def sample(self, schema, table, columns, limit):
        self.samples.append((schema, table, columns, limit))
        return columns, [[1, 10], [2, "x" * 2100]][:limit]

    def execute_readonly(self, sql, *, row_limit, timeout_ms):
        self.queries.append((sql, row_limit, timeout_ms))
        return ["amount"], [[10], [20]][:row_limit], row_limit < 2


class Factory:
    def __init__(self, database):
        self.database = database
        self.keys = []

    def get(self, datasource, credential, access_mode):
        self.keys.append((datasource.datasource_id, access_mode, datasource.updated_at))
        return self.database


def context(task_id, inline_budget=32000):
    return ToolExecutionContext(
        tenant_id="tenant_test",
        user_id="user_test",
        task_id=task_id,
        thread_id="thread",
        node_run_id="node",
        cancellation_token=Token(),
        deadline=datetime.now(timezone.utc) + timedelta(minutes=1),
        inline_budget=inline_budget,
        operation_key="c" * 64,
    )


def setup_source(container, session_factory):
    task_id = container.task_service.create(
        "tenant_test", "user_test", "data", CreateTaskRequest(query="q")
    ).projection.task_id
    with session_factory() as session, session.begin():
        session.add(
            DataSourceRow(
                datasource_id="ds1",
                tenant_id="tenant_test",
                name="analytics",
                host="db.example",
                port=5432,
                database_name="analytics",
                ssl_mode="require",
                credential_ref="secret_ref",
                enabled=True,
            )
        )
        session.add(
            DataSourceUserGrantRow(
                tenant_id="tenant_test",
                datasource_id="ds1",
                user_id="user_test",
                access_level="read",
            )
        )
        session.add(
            DataSourceTablePolicyRow(
                tenant_id="tenant_test",
                datasource_id="ds1",
                schema_name="sales",
                table_name="orders",
                readable_columns=["id", "amount"],
                writable_columns=[],
                max_write_rows=0,
            )
        )
    credentials = Credentials()
    database = Database()
    factory = Factory(database)
    service = DataSourceService(session_factory, credentials, factory)
    return task_id, service, credentials, database, factory


def test_datasource_authorization_then_credential_and_tools(make_container, session_factory) -> None:
    container, _ = make_container({})
    task_id, service, credentials, database, factory = setup_source(container, session_factory)
    source = service.authorize(
        tenant_id="tenant_test",
        user_id="user_test",
        task_id=task_id,
        datasource_id=None,
    )
    assert source.row.datasource_id == "ds1"
    assert credentials.calls == [("secret_ref", "read")]
    assert factory.keys[0][:2] == ("ds1", "read")

    schema = InspectSchemaAdapter(service).execute(context(task_id), {})
    assert schema.data["tables"] == [{"schema": "sales", "name": "orders"}]
    described = InspectSchemaAdapter(service).execute(
        context(task_id), {"table": "sales.orders"}
    )
    assert [item["name"] for item in described.data["columns"]] == ["id", "amount"]
    sampled = SampleTableAdapter(service).execute(
        context(task_id), {"table": "sales.orders", "limit": 5}
    )
    assert database.samples[0][2] == ["id", "amount"]
    assert sampled.truncated is True

    result = ExecuteReadonlySqlAdapter(service, container.workspace_service).execute(
        context(task_id),
        {
            "sql": "WITH base AS (SELECT o.amount FROM sales.orders o) SELECT amount FROM base",
            "row_limit": 200,
            "timeout_ms": 10000,
        },
    )
    assert result.ok and result.data["columns"] == ["amount"]
    assert database.queries[0][1:] == (200, 10000)


def test_sql_ast_lineage_and_policy_denials(make_container, session_factory) -> None:
    container, _ = make_container({})
    task_id, service, credentials, database, _ = setup_source(container, session_factory)
    source = service.authorize(
        tenant_id="tenant_test", user_id="user_test", task_id=task_id, datasource_id="ds1"
    )
    lineage = parse_readonly_sql(
        "SELECT o.amount FROM (SELECT id, amount FROM sales.orders) o WHERE o.id > 0",
        source.policies,
    )
    assert lineage.tables == {("sales", "orders")}
    authorize_lineage(lineage, source.policies, database)
    aggregate = parse_readonly_sql("SELECT COUNT(*) FROM sales.orders", source.policies)
    assert aggregate.stars == set()
    authorize_lineage(aggregate, source.policies, database)
    with pytest.raises(ValueError):
        parse_readonly_sql("SELECT 1; SELECT 2", source.policies)
    with pytest.raises(ValueError):
        parse_readonly_sql("DELETE FROM sales.orders", source.policies)
    with pytest.raises(PermissionError, match=r"SELECT \*"):
        authorize_lineage(
            parse_readonly_sql("SELECT * FROM sales.orders", source.policies),
            source.policies,
            database,
        )
    denied = ExecuteReadonlySqlAdapter(service, container.workspace_service).execute(
        context(task_id),
        {"sql": "SELECT secret FROM sales.orders", "row_limit": 10, "timeout_ms": 1000},
    )
    assert denied.error.type == "policy_denied"

    with session_factory() as session, session.begin():
        session.get(DataSourceRow, "ds1").enabled = False
    credentials.calls.clear()
    with pytest.raises(PermissionError):
        service.authorize(
            tenant_id="tenant_test",
            user_id="user_test",
            task_id=task_id,
            datasource_id="ds1",
        )
    assert credentials.calls == []


def test_postgres_pool_key_is_versioned_and_retires_stale_pool() -> None:
    now = datetime.now(timezone.utc)
    datasource = DataSourceRow(
        datasource_id="ds-versioned",
        tenant_id="tenant_test",
        name="versioned",
        host="db.example",
        port=5432,
        database_name="analytics",
        ssl_mode="require",
        credential_ref="secret_ref",
        enabled=True,
        updated_at=now,
    )
    factory = VersionedPostgresFactory()
    credential = DatabaseCredential("reader", "secret")
    first = factory.get(datasource, credential, "read")
    assert factory.get(datasource, credential, "read") is first

    datasource.updated_at = now + timedelta(seconds=1)
    replacement = factory.get(datasource, credential, "read")
    assert replacement is not first
    assert first.closed is True
