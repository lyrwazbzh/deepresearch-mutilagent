from __future__ import annotations

import csv
import io
import queue
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from src.agent.config import DEFAULT_PHASE6_LIMITS, Phase6Limits
from src.agent.identity import canonical_json, sha256_hex
from src.agent.backends.persistence.database import SessionFactory
from src.agent.backends.persistence.models import (
    DataSourceRow,
    DataSourceTablePolicyRow,
    DataSourceUserGrantRow,
    ResearchTaskRow,
)
from src.agent.runtime.tool_runtime import (
    ToolDefinition,
    ToolError,
    ToolExecutionContext,
    ToolReference,
    ToolResult,
)
from src.agent.backends.workspace import WorkspaceService


@dataclass(frozen=True)
class DatabaseCredential:
    username: str
    password: str


class CredentialResolver(Protocol):
    def resolve(self, credential_ref: str, mode: str) -> DatabaseCredential: ...


class TenantSecretReader(Protocol):
    def resolve_text(self, tenant_id: str, secret_ref: str) -> str: ...


class EnvironmentCredentialResolver:
    """Development resolver; production can replace it with encrypted SecretsService."""

    def resolve(self, credential_ref: str, mode: str) -> DatabaseCredential:
        import json
        import os

        raw = os.getenv(credential_ref)
        if not raw:
            raise PermissionError("Database credential is unavailable")
        bundle = json.loads(raw)
        selected = bundle.get(mode)
        if not selected:
            raise PermissionError(f"Database {mode} principal is unavailable")
        return DatabaseCredential(username=selected["username"], password=selected["password"])


class SecretsCredentialResolver:
    """JIT DataSource resolver backed by the tenant-scoped encrypted Secret Store."""

    def __init__(self, secrets: TenantSecretReader) -> None:
        self.secrets = secrets

    def resolve(self, credential_ref: str, mode: str) -> DatabaseCredential:
        del credential_ref, mode
        raise PermissionError("Tenant scope is required for Secret Store resolution")

    def resolve_for_tenant(
        self, tenant_id: str, credential_ref: str, mode: str
    ) -> DatabaseCredential:
        import json

        try:
            bundle = json.loads(self.secrets.resolve_text(tenant_id, credential_ref))
            selected = bundle.get(mode)
            if not isinstance(selected, dict):
                raise TypeError
            username = selected["username"]
            password = selected["password"]
            if not isinstance(username, str) or not isinstance(password, str):
                raise TypeError
            return DatabaseCredential(username=username, password=password)
        except PermissionError:
            raise
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise PermissionError("Database credential is unavailable") from exc


def resolve_database_credential(
    resolver: CredentialResolver,
    *,
    tenant_id: str,
    credential_ref: str,
    mode: str,
) -> DatabaseCredential:
    """Use tenant-aware production resolution while preserving test/dev resolvers."""

    scoped = getattr(resolver, "resolve_for_tenant", None)
    if callable(scoped):
        return scoped(tenant_id, credential_ref, mode)
    return resolver.resolve(credential_ref, mode)


class DatabaseEgressPolicy(Protocol):
    def validate(self, datasource: DataSourceRow) -> None: ...


class HostPortDatabaseEgressPolicy:
    """Exact host/port allowlist for database-protocol egress."""

    def __init__(self, allowed_endpoints: tuple[str, ...] | list[str]) -> None:
        self.allowed_endpoints = frozenset(
            item.strip().lower() for item in allowed_endpoints if item.strip()
        )

    def validate(self, datasource: DataSourceRow) -> None:
        endpoint = f"{datasource.host.rstrip('.').lower()}:{datasource.port}"
        if endpoint not in self.allowed_endpoints:
            raise PermissionError("DataSource endpoint is denied by database egress policy")


@dataclass(frozen=True)
class ColumnMetadata:
    schema: str
    table: str
    name: str
    type: str
    nullable: bool | None = None


@dataclass(frozen=True)
class TableMetadata:
    columns: list[ColumnMetadata]
    primary_key: list[str]
    foreign_keys: list[dict[str, Any]]


class ExternalDatabase(Protocol):
    def list_tables(self) -> list[tuple[str, str]]: ...

    def describe(self, schema: str, table: str) -> TableMetadata: ...

    def sample(
        self, schema: str, table: str, columns: list[str], limit: int
    ) -> tuple[list[str], list[list[Any]]]: ...

    def execute_readonly(
        self, sql: str, *, row_limit: int, timeout_ms: int
    ) -> tuple[list[str], list[list[Any]], bool]: ...


class PostgresDatabase:
    def __init__(
        self,
        datasource: DataSourceRow,
        credential: DatabaseCredential,
        limits: Phase6Limits = DEFAULT_PHASE6_LIMITS,
    ) -> None:
        self.datasource = datasource
        self.credential = credential
        self.limits = limits
        self._pool = PostgresConnectionPool(self._connect)
        self.closed = False

    def _connect(self) -> Any:
        import psycopg

        return psycopg.connect(
            host=self.datasource.host,
            port=self.datasource.port,
            dbname=self.datasource.database_name,
            user=self.credential.username,
            password=self.credential.password,
            sslmode=self.datasource.ssl_mode,
            connect_timeout=10,
        )

    def close(self) -> None:
        self.closed = True
        self._pool.close()

    def list_tables(self) -> list[tuple[str, str]]:
        with self._pool.connection() as connection, connection.cursor() as cursor:
            self._configure_readonly(cursor)
            cursor.execute(
                "SELECT table_schema, table_name FROM information_schema.tables "
                "WHERE table_type='BASE TABLE' ORDER BY table_schema, table_name"
            )
            return [(str(row[0]), str(row[1])) for row in cursor.fetchall()]

    def describe(self, schema: str, table: str) -> TableMetadata:
        with self._pool.connection() as connection, connection.cursor() as cursor:
            self._configure_readonly(cursor)
            cursor.execute(
                "SELECT table_schema, table_name, column_name, data_type, is_nullable "
                "FROM information_schema.columns WHERE table_schema=%s AND table_name=%s "
                "ORDER BY ordinal_position",
                (schema, table),
            )
            columns = [
                ColumnMetadata(str(s), str(t), str(c), str(dtype), nullable == "YES")
                for s, t, c, dtype, nullable in cursor.fetchall()
            ]
            cursor.execute(
                "SELECT a.attname FROM pg_index i JOIN pg_attribute a "
                "ON a.attrelid=i.indrelid AND a.attnum=ANY(i.indkey) "
                "WHERE i.indrelid=%s::regclass AND i.indisprimary ORDER BY a.attnum",
                (f'"{schema}"."{table}"',),
            )
            primary = [str(row[0]) for row in cursor.fetchall()]
            cursor.execute(
                "SELECT kcu.column_name, ccu.table_schema, ccu.table_name, ccu.column_name "
                "FROM information_schema.table_constraints tc "
                "JOIN information_schema.key_column_usage kcu ON tc.constraint_name=kcu.constraint_name AND tc.constraint_schema=kcu.constraint_schema "
                "JOIN information_schema.constraint_column_usage ccu ON ccu.constraint_name=tc.constraint_name AND ccu.constraint_schema=tc.constraint_schema "
                "WHERE tc.constraint_type='FOREIGN KEY' AND tc.table_schema=%s AND tc.table_name=%s",
                (schema, table),
            )
            foreign = [
                {
                    "columns": [str(column)],
                    "referenced_schema": str(ref_schema),
                    "referenced_table": str(ref_table),
                    "referenced_columns": [str(ref_column)],
                }
                for column, ref_schema, ref_table, ref_column in cursor.fetchall()
            ]
        return TableMetadata(columns=columns, primary_key=primary, foreign_keys=foreign)

    def sample(
        self, schema: str, table: str, columns: list[str], limit: int
    ) -> tuple[list[str], list[list[Any]]]:
        from psycopg import sql

        query = sql.SQL("SELECT {} FROM {}.{} LIMIT %s").format(
            sql.SQL(",").join(sql.Identifier(column) for column in columns),
            sql.Identifier(schema),
            sql.Identifier(table),
        )
        with self._pool.connection() as connection, connection.cursor() as cursor:
            self._configure_readonly(cursor)
            cursor.execute(query, (limit,))
            return columns, [list(row) for row in cursor.fetchall()]

    def execute_readonly(
        self, sql_text: str, *, row_limit: int, timeout_ms: int
    ) -> tuple[list[str], list[list[Any]], bool]:
        import psycopg

        with self._pool.connection() as connection, connection.cursor() as cursor:
            self._configure_readonly(cursor, timeout_ms)
            try:
                cursor.execute(sql_text)
            except psycopg.errors.QueryCanceled as exc:
                raise TimeoutError("External PostgreSQL query timed out") from exc
            columns = [item.name for item in cursor.description or []]
            rows = [list(row) for row in cursor.fetchmany(row_limit + 1)]
            return columns, rows[:row_limit], len(rows) > row_limit

    def _configure_readonly(self, cursor: Any, timeout_ms: int | None = None) -> None:
        cursor.execute("SET TRANSACTION READ ONLY")
        cursor.execute(
            "SELECT set_config('statement_timeout', %s, true)",
            (str(timeout_ms or self.limits.readonly_sql_default_timeout_ms),),
        )


class PostgresConnectionPool:
    """Small bounded pool with checkout health checks and safe retirement."""

    def __init__(self, connector: Any, *, max_size: int = 4, acquire_timeout: float = 10) -> None:
        self.connector = connector
        self.max_size = max_size
        self.acquire_timeout = acquire_timeout
        self._idle: queue.LifoQueue[Any] = queue.LifoQueue(max_size)
        self._lock = threading.Lock()
        self._created = 0
        self._closed = False

    @contextmanager
    def connection(self):
        connection = self._acquire()
        discard = False
        try:
            yield connection
        except BaseException:
            discard = bool(getattr(connection, "closed", False))
            raise
        finally:
            try:
                connection.rollback()
            except Exception:
                discard = True
            if discard or self._closed:
                self._discard(connection)
            else:
                self._idle.put(connection)

    def _acquire(self) -> Any:
        while True:
            if self._closed:
                raise RuntimeError("PostgreSQL connection pool is closed")
            try:
                connection = self._idle.get_nowait()
            except queue.Empty:
                with self._lock:
                    if self._created < self.max_size:
                        connection = self.connector()
                        self._created += 1
                    else:
                        connection = None
                if connection is None:
                    try:
                        connection = self._idle.get(timeout=self.acquire_timeout)
                    except queue.Empty as exc:
                        raise TimeoutError("PostgreSQL connection pool is exhausted") from exc
            if self._healthy(connection):
                return connection
            self._discard(connection)

    @staticmethod
    def _healthy(connection: Any) -> bool:
        if bool(getattr(connection, "closed", False)):
            return False
        try:
            with connection.cursor() as cursor:
                cursor.execute("SELECT 1")
                cursor.fetchone()
            connection.rollback()
            return True
        except Exception:
            return False

    def _discard(self, connection: Any) -> None:
        try:
            connection.close()
        finally:
            with self._lock:
                self._created = max(0, self._created - 1)

    def close(self) -> None:
        self._closed = True
        while True:
            try:
                connection = self._idle.get_nowait()
            except queue.Empty:
                return
            self._discard(connection)


class DatabaseFactory(Protocol):
    def get(
        self,
        datasource: DataSourceRow,
        credential: DatabaseCredential,
        access_mode: str,
    ) -> ExternalDatabase: ...


class VersionedPostgresFactory:
    """Connections are isolated by datasource, access mode and metadata revision."""

    def __init__(self, limits: Phase6Limits = DEFAULT_PHASE6_LIMITS) -> None:
        self.limits = limits
        self._databases: dict[tuple[str, str, datetime], ExternalDatabase] = {}
        self._lock = threading.Lock()

    def get(
        self,
        datasource: DataSourceRow,
        credential: DatabaseCredential,
        access_mode: str,
    ) -> ExternalDatabase:
        key = (datasource.datasource_id, access_mode, datasource.updated_at)
        with self._lock:
            database = self._databases.get(key)
            if database is not None:
                return database
            stale = [
                old_key
                for old_key in self._databases
                if old_key[:2] == key[:2] and old_key != key
            ]
            for old_key in stale:
                retired = self._databases.pop(old_key)
                close = getattr(retired, "close", None)
                if close is not None:
                    close()
            database = PostgresDatabase(datasource, credential, self.limits)
            self._databases[key] = database
            return database


@dataclass(frozen=True)
class AuthorizedDataSource:
    row: DataSourceRow
    database: ExternalDatabase
    policies: dict[tuple[str, str], DataSourceTablePolicyRow]


class DataSourceService:
    def __init__(
        self,
        session_factory: SessionFactory,
        credentials: CredentialResolver,
        databases: DatabaseFactory,
        egress_policy: DatabaseEgressPolicy | None = None,
    ) -> None:
        self.session_factory = session_factory
        self.credentials = credentials
        self.databases = databases
        self.egress_policy = egress_policy

    def authorize(
        self,
        *,
        tenant_id: str,
        user_id: str,
        task_id: str,
        datasource_id: str | None,
    ) -> AuthorizedDataSource:
        with self.session_factory() as session:
            task = session.scalar(
                select(ResearchTaskRow).where(
                    ResearchTaskRow.tenant_id == tenant_id,
                    ResearchTaskRow.user_id == user_id,
                    ResearchTaskRow.task_id == task_id,
                )
            )
            if task is None:
                raise PermissionError("Task is outside the current scope")
            scoped = set((task.capability_snapshot or {}).get("datasource_ids") or [])
            rows = list(
                session.execute(
                    select(DataSourceRow, DataSourceUserGrantRow)
                    .join(
                        DataSourceUserGrantRow,
                        (DataSourceUserGrantRow.datasource_id == DataSourceRow.datasource_id)
                        & (DataSourceUserGrantRow.tenant_id == DataSourceRow.tenant_id),
                    )
                    .where(
                        DataSourceRow.tenant_id == tenant_id,
                        DataSourceRow.enabled.is_(True),
                        DataSourceUserGrantRow.user_id == user_id,
                        DataSourceUserGrantRow.access_level.in_(["read", "write"]),
                    )
                ).all()
            )
            if scoped:
                rows = [pair for pair in rows if pair[0].datasource_id in scoped]
            if datasource_id:
                rows = [pair for pair in rows if pair[0].datasource_id == datasource_id]
            if not rows:
                raise PermissionError("DataSource is not authorized")
            if len(rows) > 1 and datasource_id is None:
                raise ValueError("datasource_id is required when multiple sources are authorized")
            row = rows[0][0]
            policies = list(
                session.scalars(
                    select(DataSourceTablePolicyRow).where(
                        DataSourceTablePolicyRow.tenant_id == tenant_id,
                        DataSourceTablePolicyRow.datasource_id == row.datasource_id,
                    )
                ).all()
            )
            for item in [row, *policies]:
                session.expunge(item)
        if self.egress_policy is not None:
            self.egress_policy.validate(row)
        # JIT secret resolution happens only after all task/tenant/grant/egress checks above.
        credential = resolve_database_credential(
            self.credentials,
            tenant_id=tenant_id,
            credential_ref=row.credential_ref,
            mode="read",
        )
        database = self.databases.get(row, credential, "read")
        return AuthorizedDataSource(
            row=row,
            database=database,
            policies={(policy.schema_name, policy.table_name): policy for policy in policies},
        )


@dataclass(frozen=True)
class SqlLineage:
    tables: set[tuple[str, str]]
    columns: dict[tuple[str, str], set[str]]
    stars: set[tuple[str, str]]


def parse_readonly_sql(sql_text: str, policies: dict[tuple[str, str], DataSourceTablePolicyRow]) -> SqlLineage:
    try:
        from sqlglot import exp, parse
    except ImportError as exc:  # pragma: no cover - packaging dependency
        raise RuntimeError("sqlglot is required for SQL policy validation") from exc
    statements = parse(sql_text, read="postgres")
    if len(statements) != 1:
        raise ValueError("exactly one SQL statement is required")
    tree = statements[0]
    if not isinstance(tree, (exp.Select, exp.Union, exp.Intersect, exp.Except)):
        raise ValueError("only SELECT or read-only WITH queries are allowed")
    if any(tree.find(kind) is not None for kind in (exp.Insert, exp.Update, exp.Delete, exp.Create, exp.Drop, exp.Command)):
        raise ValueError("SQL statement is not read-only")
    forbidden_functions = {
        "dblink",
        "dblink_connect",
        "lo_export",
        "lo_import",
        "pg_file_read",
        "pg_ls_dir",
        "pg_read_binary_file",
        "pg_read_file",
        "pg_stat_file",
    }
    for function in tree.find_all(exp.Func):
        name = str(getattr(function, "name", "") or function.sql_name()).lower()
        if name in forbidden_functions:
            raise PermissionError(f"unsafe SQL function is denied: {name}")
    cte_names = {cte.alias_or_name for cte in tree.find_all(exp.CTE)}
    aliases: dict[str, tuple[str, str]] = {}
    tables: set[tuple[str, str]] = set()
    for table_node in tree.find_all(exp.Table):
        if table_node.name in cte_names and not table_node.db:
            continue
        identity = (table_node.db or "public", table_node.name)
        if identity[0].lower() in {"information_schema", "pg_catalog", "pg_toast"}:
            raise PermissionError("system catalog access is denied")
        tables.add(identity)
        aliases[table_node.alias_or_name] = identity
        aliases[table_node.name] = identity
    if not tables:
        raise ValueError("query must read an authorized table")
    columns: dict[tuple[str, str], set[str]] = {table: set() for table in tables}
    stars: set[tuple[str, str]] = set()
    for star in tree.find_all(exp.Star):
        parent = star.parent
        if not isinstance(parent, (exp.Column, exp.Select)):
            # COUNT(*) and similar aggregate stars do not expose table columns.
            continue
        qualifier = parent.table if isinstance(parent, exp.Column) else ""
        targets = [aliases[qualifier]] if qualifier in aliases else list(tables)
        stars.update(targets)
    for column in tree.find_all(exp.Column):
        if isinstance(column.this, exp.Star):
            targets = [aliases[column.table]] if column.table in aliases else list(tables)
            stars.update(targets)
            continue
        if column.table:
            target = aliases.get(column.table)
            if target:
                columns[target].add(column.name)
            continue
        candidates = []
        for table_identity in tables:
            policy = policies.get(table_identity)
            if policy is not None and column.name in set(policy.readable_columns):
                candidates.append(table_identity)
        if len(candidates) != 1:
            raise PermissionError(f"unqualified or unauthorized column: {column.name}")
        columns[candidates[0]].add(column.name)
    return SqlLineage(tables=tables, columns=columns, stars=stars)


def authorize_lineage(
    lineage: SqlLineage,
    policies: dict[tuple[str, str], DataSourceTablePolicyRow],
    database: ExternalDatabase,
) -> None:
    for table in lineage.tables:
        policy = policies.get(table)
        if policy is None:
            raise PermissionError(f"table is not readable: {table[0]}.{table[1]}")
        readable = set(policy.readable_columns)
        if not lineage.columns[table].issubset(readable):
            raise PermissionError(f"column policy denied: {table[0]}.{table[1]}")
        if table in lineage.stars:
            actual = {item.name for item in database.describe(*table).columns}
            if actual != readable:
                raise PermissionError("SELECT * is denied when readable columns are partial")


class InspectSchemaArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    datasource_id: str | None = None
    table: str | None = None
    limit: int = Field(default=100, ge=1, le=500)
    cursor: int | None = Field(default=None, ge=0)


class SampleTableArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    datasource_id: str | None = None
    table: str
    limit: int = Field(default=5, ge=1, le=20)


class ExecuteReadonlySqlArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    sql: str = Field(min_length=1)
    datasource_id: str | None = None
    row_limit: int = Field(
        default=DEFAULT_PHASE6_LIMITS.readonly_sql_default_rows,
        ge=1,
        le=DEFAULT_PHASE6_LIMITS.readonly_sql_max_rows,
    )
    timeout_ms: int = Field(
        default=DEFAULT_PHASE6_LIMITS.readonly_sql_default_timeout_ms,
        ge=1,
        le=DEFAULT_PHASE6_LIMITS.readonly_sql_max_timeout_ms,
    )


class DataToolBase:
    def __init__(self, sources: DataSourceService) -> None:
        self.sources = sources

    def source(self, context: ToolExecutionContext, arguments: dict[str, Any]) -> AuthorizedDataSource:
        return self.sources.authorize(
            tenant_id=context.tenant_id,
            user_id=context.user_id,
            task_id=context.task_id,
            datasource_id=arguments.get("datasource_id"),
        )


class InspectSchemaAdapter(DataToolBase):
    def execute(self, context: ToolExecutionContext, arguments: dict[str, Any]) -> ToolResult:
        try:
            source = self.source(context, arguments)
            if not arguments.get("table"):
                allowed = sorted(set(source.database.list_tables()) & set(source.policies))
                start = int(arguments.get("cursor") or 0)
                if start < 0:
                    raise ValueError("cursor must be non-negative")
                selected = allowed[start : start + arguments.get("limit", 100)]
                return ToolResult(
                    status="success",
                    summary=f"{len(selected)} authorized tables",
                    data={
                        "datasource_id": source.row.datasource_id,
                        "tables": [{"schema": schema, "name": table} for schema, table in selected],
                        "truncated": start + len(selected) < len(allowed),
                    },
                    next_cursor=str(start + len(selected)) if start + len(selected) < len(allowed) else None,
                )
            schema, table = _table(arguments["table"])
            policy = source.policies.get((schema, table))
            if policy is None:
                raise PermissionError("table policy denied")
            metadata = source.database.describe(schema, table)
            readable = set(policy.readable_columns)
            columns = [item for item in metadata.columns if item.name in readable]
            primary = [column for column in metadata.primary_key if column in readable]
            foreign = [
                fk
                for fk in metadata.foreign_keys
                if set(fk["columns"]).issubset(readable)
                and (fk["referenced_schema"], fk["referenced_table"]) in source.policies
            ]
            return ToolResult(
                status="success",
                summary=f"{schema}.{table} schema",
                data={
                    "datasource_id": source.row.datasource_id,
                    "columns": [item.__dict__ for item in columns],
                    "primary_key": primary,
                    "foreign_keys": foreign,
                },
            )
        except (ValueError, PermissionError) as exc:
            return _data_error(exc)


class SampleTableAdapter(DataToolBase):
    def execute(self, context: ToolExecutionContext, arguments: dict[str, Any]) -> ToolResult:
        try:
            source = self.source(context, arguments)
            schema, table = _table(arguments["table"])
            policy = source.policies.get((schema, table))
            if policy is None:
                raise PermissionError("table policy denied")
            columns = list(policy.readable_columns)
            returned, rows = source.database.sample(
                schema, table, columns, arguments.get("limit", 5)
            )
            truncated = False
            normalized = []
            for row in rows:
                output_row = []
                for value in row:
                    if isinstance(value, str) and len(value) > 2000:
                        output_row.append(value[:2000])
                        truncated = True
                    else:
                        output_row.append(value)
                normalized.append(output_row)
            return ToolResult(
                status="success",
                summary=f"{len(normalized)} sample rows from {schema}.{table}",
                data={
                    "datasource_id": source.row.datasource_id,
                    "table": f"{schema}.{table}",
                    "columns": returned,
                    "rows": normalized,
                    "truncated": truncated,
                },
                truncated=truncated,
            )
        except (ValueError, PermissionError) as exc:
            return _data_error(exc)


class ExecuteReadonlySqlAdapter(DataToolBase):
    def __init__(
        self,
        sources: DataSourceService,
        workspace: WorkspaceService,
        limits: Phase6Limits = DEFAULT_PHASE6_LIMITS,
    ) -> None:
        super().__init__(sources)
        self.workspace = workspace
        self.limits = limits

    def execute(self, context: ToolExecutionContext, arguments: dict[str, Any]) -> ToolResult:
        try:
            source = self.source(context, arguments)
            lineage = parse_readonly_sql(arguments["sql"], source.policies)
            authorize_lineage(lineage, source.policies, source.database)
            row_limit = int(
                arguments.get("row_limit", self.limits.readonly_sql_default_rows)
            )
            timeout_ms = int(
                arguments.get(
                    "timeout_ms", self.limits.readonly_sql_default_timeout_ms
                )
            )
            if not 1 <= row_limit <= self.limits.readonly_sql_max_rows:
                raise ValueError("row_limit exceeds the configured read-only SQL limit")
            if not 1 <= timeout_ms <= self.limits.readonly_sql_max_timeout_ms:
                raise ValueError("timeout_ms exceeds the configured read-only SQL limit")
            columns, rows, truncated = source.database.execute_readonly(
                arguments["sql"],
                row_limit=row_limit,
                timeout_ms=timeout_ms,
            )
            data = {"columns": columns, "rows": rows, "truncated": truncated}
            encoded = canonical_json(data)
            if len(encoded) <= context.inline_budget:
                digest = sha256_hex(encoded)
                return ToolResult(
                    status="success",
                    summary=f"{len(rows)} rows",
                    data=data,
                    references=[ToolReference(content_hash=digest)],
                    truncated=truncated,
                )
            stream = io.StringIO()
            writer = csv.writer(stream)
            writer.writerow(columns)
            writer.writerows(rows)
            payload = stream.getvalue().encode("utf-8")
            file = self.workspace.publish_bytes(
                tenant_id=context.tenant_id,
                task_id=context.task_id,
                path=f"files/sql/{context.operation_key[:24]}.csv",
                data=payload,
                mime_type="text/csv; charset=utf-8",
                operation_key=context.operation_key,
            )
            return ToolResult(
                status="success",
                summary=f"{len(rows)} rows; result stored in Workspace",
                references=[
                    ToolReference(
                        workspace_path=file.workspace_path,
                        content_hash=file.content_hash,
                        mime_type=file.mime_type,
                        size=file.size,
                    )
                ],
                truncated=truncated,
            )
        except (ValueError, PermissionError) as exc:
            return _data_error(exc)


def tool_definitions(
    sources: DataSourceService,
    workspace: WorkspaceService,
    limits: Phase6Limits = DEFAULT_PHASE6_LIMITS,
) -> list[ToolDefinition]:
    return [
        ToolDefinition(
            name="inspect_schema",
            args_schema=InspectSchemaArgs,
            adapter=InspectSchemaAdapter(sources),
        ),
        ToolDefinition(
            name="sample_table",
            args_schema=SampleTableArgs,
            adapter=SampleTableAdapter(sources),
        ),
        ToolDefinition(
            name="execute_readonly_sql",
            args_schema=ExecuteReadonlySqlArgs,
            adapter=ExecuteReadonlySqlAdapter(sources, workspace, limits),
            evidence_eligible=True,
            effect_target=lambda args: sha256_hex(
                canonical_json(
                    {
                        "datasource_id": args.get("datasource_id"),
                        "sql": args["sql"],
                        "row_limit": args.get(
                            "row_limit", limits.readonly_sql_default_rows
                        ),
                    }
                )
            ),
        ),
    ]


def _table(value: str) -> tuple[str, str]:
    pieces = value.split(".")
    if len(pieces) != 2 or not all(piece and piece.replace("_", "a").isalnum() for piece in pieces):
        raise ValueError("table must use schema.table")
    return pieces[0], pieces[1]


def _data_error(exc: Exception) -> ToolResult:
    denied = isinstance(exc, PermissionError)
    return ToolResult(
        status="error",
        error=ToolError(
            type="policy_denied" if denied else "invalid_argument",
            code="policy_denied" if denied else "invalid_argument",
            message=str(exc),
        ),
    )
