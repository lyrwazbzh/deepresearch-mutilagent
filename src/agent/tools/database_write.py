from __future__ import annotations

import hashlib
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Protocol

from sqlalchemy import select

from src.agent.tools.database_read import (
    CredentialResolver,
    DatabaseCredential,
    DatabaseEgressPolicy,
    PostgresConnectionPool,
    resolve_database_credential,
)
from src.agent.identity import canonical_json, sha256_hex
from src.agent.schema.state import ResearchState
from src.agent.schema.write import (
    PendingWrite,
    WriteDecision,
    WritePlan,
    WritePreview,
    WriteResult,
)
from src.agent.backends.persistence.database import SessionFactory
from src.agent.backends.persistence.models import (
    AuditLogRow,
    DataSourceRow,
    DataSourceTablePolicyRow,
    DataSourceUserGrantRow,
    OutboxMessageRow,
    PendingWriteRow,
    ResearchCheckpointRow,
    ResearchEventRow,
    ResearchTaskCommandRow,
    ResearchTaskRow,
    utcnow,
)
from src.agent.backends.persistence.repositories import CheckpointRepository
from src.agent.errors import (
    IdempotencyConflictError,
    InvalidStateError,
    NotFoundError,
    PolicyDeniedError,
)
from src.agent.task_service import new_id


class PlanHashMismatchError(IdempotencyConflictError):
    code = "plan_hash_mismatch"
    title = "Plan hash mismatch"


class StaleWritePlanError(RuntimeError):
    pass


class WriteLimitExceededError(RuntimeError):
    pass


class IdempotencyLedgerConflictError(RuntimeError):
    pass


class DatabaseWriteFailedError(RuntimeError):
    pass


@dataclass(frozen=True)
class WritePolicy:
    datasource: DataSourceRow
    table: DataSourceTablePolicyRow
    revision: str


@dataclass(frozen=True)
class PreparedWrite:
    write_id: str
    plan: WritePlan
    preview: WritePreview
    plan_hash: str
    policy_revision: str
    operation_key: str


@dataclass(frozen=True)
class CompiledWrite:
    sql: str
    parameters: tuple[Any, ...]


class WritePolicyService:
    def __init__(self, session_factory: SessionFactory) -> None:
        self.session_factory = session_factory

    @staticmethod
    def split_table(value: str, default_schema: str = "public") -> tuple[str, str]:
        parts = value.split(".")
        if len(parts) == 1 and parts[0]:
            return default_schema, parts[0]
        if len(parts) != 2 or not all(parts):
            raise ValueError("table must be table or schema.table")
        return parts[0], parts[1]

    @staticmethod
    def revision(datasource: DataSourceRow, policy: DataSourceTablePolicyRow) -> str:
        return sha256_hex(
            canonical_json(
                {
                    "datasource_id": datasource.datasource_id,
                    "datasource_updated_at": datasource.updated_at.isoformat(),
                    "schema_name": policy.schema_name,
                    "table_name": policy.table_name,
                    "readable_columns": sorted(policy.readable_columns),
                    "writable_columns": sorted(policy.writable_columns),
                    "allow_insert": policy.allow_insert,
                    "allow_update": policy.allow_update,
                    "allow_delete": policy.allow_delete,
                    "max_write_rows": policy.max_write_rows,
                }
            )
        )

    def resolve(
        self, *, tenant_id: str, user_id: str, task_id: str, plan: WritePlan
    ) -> WritePolicy:
        parts = plan.table.split(".")
        schema, table = self.split_table(plan.table)
        with self.session_factory() as session:
            task = session.scalar(
                select(ResearchTaskRow).where(
                    ResearchTaskRow.tenant_id == tenant_id,
                    ResearchTaskRow.user_id == user_id,
                    ResearchTaskRow.task_id == task_id,
                )
            )
            source = session.scalar(
                select(DataSourceRow).where(
                    DataSourceRow.tenant_id == tenant_id,
                    DataSourceRow.datasource_id == plan.datasource_id,
                    DataSourceRow.enabled.is_(True),
                )
            )
            grant = session.scalar(
                select(DataSourceUserGrantRow).where(
                    DataSourceUserGrantRow.tenant_id == tenant_id,
                    DataSourceUserGrantRow.datasource_id == plan.datasource_id,
                    DataSourceUserGrantRow.user_id == user_id,
                    DataSourceUserGrantRow.access_level == "write",
                )
            )
            policy_rows = list(session.scalars(
                select(DataSourceTablePolicyRow).where(
                    DataSourceTablePolicyRow.tenant_id == tenant_id,
                    DataSourceTablePolicyRow.datasource_id == plan.datasource_id,
                    DataSourceTablePolicyRow.table_name == table,
                    *(
                        [DataSourceTablePolicyRow.schema_name == schema]
                        if len(parts) == 2
                        else []
                    ),
                )
            ).all())
            policy = policy_rows[0] if len(policy_rows) == 1 else None
            scope = set((task.capability_snapshot if task else {}).get("datasource_ids") or [])
            if task is None or source is None or grant is None or policy is None:
                raise PolicyDeniedError("Current database write authorization is not valid.")
            if scope and source.datasource_id not in scope:
                raise PolicyDeniedError("DataSource is outside the task capability scope.")
            self._validate_plan(plan, policy)
            for row in (source, policy):
                session.expunge(row)
        return WritePolicy(source, policy, self.revision(source, policy))

    def metadata_scope(
        self, tenant_id: str, user_id: str, task_id: str, datasource_id: str | None
    ) -> list[dict[str, Any]]:
        with self.session_factory() as session:
            task = session.scalar(
                select(ResearchTaskRow).where(
                    ResearchTaskRow.tenant_id == tenant_id,
                    ResearchTaskRow.user_id == user_id,
                    ResearchTaskRow.task_id == task_id,
                )
            )
            if task is None:
                return []
            scope = set((task.capability_snapshot or {}).get("datasource_ids") or [])
            sources = list(
                session.scalars(
                    select(DataSourceRow)
                    .join(
                        DataSourceUserGrantRow,
                        (DataSourceUserGrantRow.tenant_id == DataSourceRow.tenant_id)
                        & (DataSourceUserGrantRow.datasource_id == DataSourceRow.datasource_id),
                    )
                    .where(
                        DataSourceRow.tenant_id == tenant_id,
                        DataSourceRow.enabled.is_(True),
                        DataSourceUserGrantRow.user_id == user_id,
                        DataSourceUserGrantRow.access_level == "write",
                        *(
                            [DataSourceRow.datasource_id == datasource_id]
                            if datasource_id
                            else []
                        ),
                    )
                ).all()
            )
            if scope:
                sources = [item for item in sources if item.datasource_id in scope]
            result: list[dict[str, Any]] = []
            for source in sources:
                policies = list(
                    session.scalars(
                        select(DataSourceTablePolicyRow).where(
                            DataSourceTablePolicyRow.tenant_id == tenant_id,
                            DataSourceTablePolicyRow.datasource_id == source.datasource_id,
                        )
                    ).all()
                )
                result.append(
                    {
                        "datasource_id": source.datasource_id,
                        "name": source.name,
                        "tables": [
                            {
                                "table": f"{item.schema_name}.{item.table_name}",
                                "readable_columns": item.readable_columns,
                                "writable_columns": item.writable_columns,
                                "allow_insert": item.allow_insert,
                                "allow_update": item.allow_update,
                                "allow_delete": item.allow_delete,
                            }
                            for item in policies
                        ],
                    }
                )
            return result

    @staticmethod
    def _validate_plan(plan: WritePlan, policy: DataSourceTablePolicyRow) -> None:
        allowed = {
            "insert": policy.allow_insert,
            "update": policy.allow_update,
            "delete": policy.allow_delete,
        }[plan.operation]
        if not allowed or policy.max_write_rows < 1:
            raise PolicyDeniedError(f"{plan.operation} is not allowed by the table policy.")
        writable = set(policy.writable_columns)
        value_columns = set(plan.values or {})
        filter_columns = {item.column for item in plan.filters or []}
        precondition_columns = set(plan.preconditions or {})
        forbidden_values = value_columns - writable
        filterable = writable | set(policy.readable_columns)
        forbidden_filters = (filter_columns | precondition_columns) - filterable
        if forbidden_values:
            raise PolicyDeniedError(f"Columns are not writable: {sorted(forbidden_values)}")
        if forbidden_filters:
            raise PolicyDeniedError(f"Columns are not filterable: {sorted(forbidden_filters)}")


class PrepareWriteService:
    def __init__(self, policies: WritePolicyService) -> None:
        self.policies = policies

    def prepare(
        self, *, tenant_id: str, user_id: str, task_id: str, plan: WritePlan
    ) -> PreparedWrite:
        policy = self.policies.resolve(
            tenant_id=tenant_id, user_id=user_id, task_id=task_id, plan=plan
        )
        normalized = WritePlan.model_validate(plan.model_dump(mode="json"))
        preview = WritePreview(
            datasource=policy.datasource.name,
            operation=normalized.operation,
            table=normalized.table,
            filters=normalized.filters,
            values=normalized.values,
            reason=normalized.reason,
            expected_value=(
                next(iter(normalized.preconditions.values()))
                if normalized.preconditions and len(normalized.preconditions) == 1
                else None
            ),
        )
        plan_hash = sha256_hex(
            canonical_json(
                {
                    "plan": normalized.model_dump(mode="json", exclude={"preconditions"}),
                    "preconditions": normalized.preconditions or {},
                    "policy_revision": policy.revision,
                }
            )
        )
        write_id = f"write_{sha256_hex(f'{tenant_id}|{task_id}|{plan_hash}')[:32]}"
        operation_key = hashlib.sha256(
            f"{tenant_id}|{write_id}|database_write:v1".encode("utf-8")
        ).hexdigest()
        return PreparedWrite(
            write_id, normalized, preview, plan_hash, policy.revision, operation_key
        )


class SqlCompiler:
    OPERATORS = {"=", "!=", ">", ">=", "<", "<=", "IN"}

    @staticmethod
    def quote(identifier: str) -> str:
        if not identifier or "\x00" in identifier:
            raise ValueError("invalid SQL identifier")
        return '"' + identifier.replace('"', '""') + '"'

    def compile(self, plan: WritePlan, *, schema_name: str | None = None) -> CompiledWrite:
        schema, table = WritePolicyService.split_table(plan.table, schema_name or "public")
        target = f"{self.quote(schema)}.{self.quote(table)}"
        params: list[Any] = []
        if plan.operation == "insert":
            columns = sorted((plan.values or {}).keys())
            params.extend((plan.values or {})[column] for column in columns)
            sql = (
                f"INSERT INTO {target} ({', '.join(self.quote(c) for c in columns)}) "
                f"VALUES ({', '.join('%s' for _ in columns)})"
            )
            return CompiledWrite(sql, tuple(params))
        where: list[str] = []
        for item in plan.filters or []:
            if item.op not in self.OPERATORS:
                raise ValueError("unsupported filter operator")
            if item.op == "IN":
                values = list(item.value)
                where.append(
                    f"{self.quote(item.column)} IN ({', '.join('%s' for _ in values)})"
                )
                params.extend(values)
            else:
                where.append(f"{self.quote(item.column)} {item.op} %s")
                params.append(item.value)
        for column, expected_value in sorted((plan.preconditions or {}).items()):
            where.append(f"{self.quote(column)} = %s")
            params.append(expected_value)
        if not where:
            raise ValueError("update/delete require a WHERE clause")
        if plan.operation == "update":
            columns = sorted((plan.values or {}).keys())
            value_params = [(plan.values or {})[column] for column in columns]
            sql = (
                f"UPDATE {target} SET "
                + ", ".join(f"{self.quote(c)} = %s" for c in columns)
                + " WHERE "
                + " AND ".join(where)
            )
            return CompiledWrite(sql, tuple([*value_params, *params]))
        return CompiledWrite(f"DELETE FROM {target} WHERE " + " AND ".join(where), tuple(params))


class WriteTarget(Protocol):
    def execute(
        self,
        *,
        compiled: CompiledWrite,
        write_id: str,
        operation_key: str,
        plan_hash: str,
        max_write_rows: int,
        has_preconditions: bool,
    ) -> WriteResult: ...


class PostgresWriteTarget:
    def __init__(
        self,
        datasource: DataSourceRow,
        credential: DatabaseCredential,
        *,
        ledger_schema: str = "public",
        ledger_table: str = "agent_write_idempotency",
    ) -> None:
        self.datasource = datasource
        self.credential = credential
        self.ledger_schema = ledger_schema
        self.ledger_table = ledger_table
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

    def execute(
        self,
        *,
        compiled: CompiledWrite,
        write_id: str,
        operation_key: str,
        plan_hash: str,
        max_write_rows: int,
        has_preconditions: bool,
    ) -> WriteResult:
        import psycopg
        from psycopg import sql
        from psycopg.types.json import Jsonb

        ledger = sql.SQL("{}.{}").format(
            sql.Identifier(self.ledger_schema),
            sql.Identifier(self.ledger_table),
        )

        with self._pool.connection() as connection:
            try:
                with connection.transaction(), connection.cursor() as cursor:
                    cursor.execute(
                        sql.SQL(
                            "INSERT INTO {} "
                            "(operation_key, write_id, plan_hash, status, affected_rows, "
                            "result_hash, result_json, created_at, updated_at, committed_at) "
                            "VALUES (%s, %s, %s, 'executing', 0, NULL, %s, now(), now(), NULL) "
                            "ON CONFLICT (operation_key) DO NOTHING"
                        ).format(ledger),
                        (
                            operation_key,
                            write_id,
                            plan_hash,
                            Jsonb({"status": "executing"}),
                        ),
                    )
                    if cursor.rowcount == 0:
                        cursor.execute(
                            sql.SQL(
                                "SELECT plan_hash, affected_rows, result_json, committed_at, "
                                "status, write_id FROM {} WHERE operation_key=%s FOR UPDATE"
                            ).format(ledger),
                            (operation_key,),
                        )
                        row = cursor.fetchone()
                        if (
                            row is None
                            or str(row[0]) != plan_hash
                            or str(row[5]) != write_id
                        ):
                            raise IdempotencyLedgerConflictError(
                                "external ledger plan hash conflict"
                            )
                        if str(row[4]) != "executed":
                            raise IdempotencyLedgerConflictError(
                                "external ledger contains a non-terminal operation"
                            )
                        result = WriteResult.model_validate(row[2])
                        return result.model_copy(update={"replayed": True})
                    cursor.execute(compiled.sql, compiled.parameters)
                    affected = max(0, cursor.rowcount)
                    if affected > max_write_rows:
                        raise WriteLimitExceededError("write_limit_exceeded")
                    if affected == 0 and has_preconditions:
                        raise StaleWritePlanError("stale_write_plan")
                    result = WriteResult(
                        status="executed",
                        affected_rows=affected,
                        operation_key=operation_key,
                        committed_at=datetime.now(timezone.utc),
                    )
                    result_json = result.model_dump(mode="json")
                    result_hash = sha256_hex(canonical_json(result_json))
                    cursor.execute(
                        sql.SQL(
                            "UPDATE {} SET status='executed', affected_rows=%s, "
                            "result_hash=%s, result_json=%s, updated_at=now(), committed_at=%s "
                            "WHERE operation_key=%s"
                        ).format(ledger),
                        (
                            affected,
                            result_hash,
                            Jsonb(result_json),
                            result.committed_at,
                            operation_key,
                        ),
                    )
            except (psycopg.DataError, psycopg.IntegrityError, psycopg.ProgrammingError) as exc:
                raise DatabaseWriteFailedError("database_write_failed") from exc
            return result


class WriteTargetFactory(Protocol):
    def create(self, datasource: DataSourceRow, credential: DatabaseCredential) -> WriteTarget: ...


class PostgresWriteTargetFactory:
    def __init__(
        self,
        *,
        ledger_schema: str = "public",
        ledger_table: str = "agent_write_idempotency",
    ) -> None:
        self.ledger_schema = ledger_schema
        self.ledger_table = ledger_table
        self._lock = threading.Lock()
        self._targets: dict[tuple[str, str, str], PostgresWriteTarget] = {}

    def create(self, datasource: DataSourceRow, credential: DatabaseCredential) -> WriteTarget:
        revision = datasource.updated_at.isoformat()
        key = (datasource.datasource_id, revision, credential.username)
        with self._lock:
            target = self._targets.get(key)
            if target is None:
                target = PostgresWriteTarget(
                    datasource,
                    credential,
                    ledger_schema=self.ledger_schema,
                    ledger_table=self.ledger_table,
                )
                self._targets[key] = target
            stale = [
                item
                for item in self._targets
                if item[0] == datasource.datasource_id and item != key
            ]
            for item in stale:
                self._targets.pop(item).close()
            return target


class PendingWriteService:
    def __init__(self, session_factory: SessionFactory) -> None:
        self.session_factory = session_factory

    @staticmethod
    def project(row: PendingWriteRow) -> PendingWrite:
        return PendingWrite.model_validate(
            {
                "write_id": row.write_id,
                "task_id": row.task_id,
                "tenant_id": row.tenant_id,
                "user_id": row.user_id,
                "plan": row.plan_json,
                "plan_hash": row.plan_hash,
                "preview": row.preview_json,
                "policy_revision": row.policy_revision,
                "status": row.status,
                "created_at": row.created_at,
                "expires_at": row.expires_at,
                "decision": row.decision_json,
                "operation_key": row.operation_key,
                "result": row.result_json,
            }
        )

    def get(self, tenant_id: str, user_id: str, task_id: str, write_id: str) -> PendingWrite:
        with self.session_factory() as session:
            row = self._owned(session, tenant_id, user_id, task_id, write_id)
            session.expunge(row)
            return self.project(row)

    @staticmethod
    def _owned(session: Any, tenant_id: str, user_id: str, task_id: str, write_id: str) -> PendingWriteRow:
        task = session.scalar(
            select(ResearchTaskRow).where(
                ResearchTaskRow.tenant_id == tenant_id,
                ResearchTaskRow.user_id == user_id,
                ResearchTaskRow.task_id == task_id,
                ResearchTaskRow.deleted_at.is_(None),
            )
        )
        row = session.scalar(
            select(PendingWriteRow).where(
                PendingWriteRow.tenant_id == tenant_id,
                PendingWriteRow.user_id == user_id,
                PendingWriteRow.task_id == task_id,
                PendingWriteRow.write_id == write_id,
            )
        )
        if task is None or row is None:
            raise NotFoundError("Pending write was not found.")
        return row

    def approve(
        self,
        *,
        tenant_id: str,
        user_id: str,
        task_id: str,
        write_id: str,
        plan_hash: str,
        comment: str | None,
        idempotency_key: str | None = None,
    ) -> PendingWrite:
        now = utcnow()
        request_hash = sha256_hex(
            canonical_json(
                {
                    "command_type": "approve",
                    "task_id": task_id,
                    "write_id": write_id,
                    "plan_hash": plan_hash,
                    "comment": comment,
                }
            )
        )
        with self.session_factory() as session:
            preliminary = self._owned(session, tenant_id, user_id, task_id, write_id)
            hash_matches = preliminary.plan_hash == plan_hash
        if not hash_matches:
            with self.session_factory() as session, session.begin():
                self._audit(
                    session, tenant_id, user_id, "pending_write.approve", write_id,
                    "denied", "plan_hash_mismatch",
                )
            raise PlanHashMismatchError("Approval does not match the prepared plan.")
        with self.session_factory() as session, session.begin():
            replay = self._command_replay(
                session,
                tenant_id,
                user_id,
                task_id,
                idempotency_key,
                request_hash,
            )
            if replay is not None:
                return replay
            row = self._owned(session, tenant_id, user_id, task_id, write_id)
            session.refresh(row, with_for_update=True)
            replay = self._command_replay(
                session,
                tenant_id,
                user_id,
                task_id,
                idempotency_key,
                request_hash,
            )
            if replay is not None:
                return replay
            if row.plan_hash != plan_hash:
                raise PlanHashMismatchError("Approval does not match the prepared plan.")
            if row.status in {"approved", "executing", "executed"}:
                projected = self.project(row)
                self._record_command(
                    session,
                    tenant_id,
                    user_id,
                    task_id,
                    idempotency_key,
                    "approve",
                    request_hash,
                    projected,
                    now,
                )
                return projected
            if row.status != "pending":
                raise InvalidStateError(f"Pending write status {row.status} cannot be approved.")
            task = session.scalar(
                select(ResearchTaskRow).where(ResearchTaskRow.task_id == task_id).with_for_update()
            )
            if task is None or task.status != "paused":
                raise InvalidStateError("Task must remain paused during write approval.")
            row.status = "approved"
            row.decision_json = WriteDecision(
                action="approved", actor_id=user_id, at=now, comment=comment
            ).model_dump(mode="json")
            row.updated_at = now
            self._audit(session, tenant_id, user_id, "pending_write.approve", write_id, "succeeded")
            session.add(
                OutboxMessageRow(
                    message_id=new_id("outbox"), tenant_id=tenant_id,
                    kind="database_write.execute", aggregate_id=write_id,
                    payload_json={"write_id": write_id, "plan_hash": plan_hash},
                    idempotency_key=f"write:{write_id}:execute", available_at=now,
                )
            )
            self._event(session, task, ["pending_write"])
            session.flush()
            projected = self.project(row)
            self._record_command(
                session,
                tenant_id,
                user_id,
                task_id,
                idempotency_key,
                "approve",
                request_hash,
                projected,
                now,
            )
            return projected

    def reject(
        self,
        *,
        tenant_id: str,
        user_id: str,
        task_id: str,
        write_id: str,
        reason: str | None,
        idempotency_key: str | None = None,
    ) -> PendingWrite:
        now = utcnow()
        request_hash = sha256_hex(
            canonical_json(
                {
                    "command_type": "reject",
                    "task_id": task_id,
                    "write_id": write_id,
                    "reason": reason,
                }
            )
        )
        with self.session_factory() as session, session.begin():
            replay = self._command_replay(
                session,
                tenant_id,
                user_id,
                task_id,
                idempotency_key,
                request_hash,
            )
            if replay is not None:
                return replay
            row = self._owned(session, tenant_id, user_id, task_id, write_id)
            session.refresh(row, with_for_update=True)
            replay = self._command_replay(
                session,
                tenant_id,
                user_id,
                task_id,
                idempotency_key,
                request_hash,
            )
            if replay is not None:
                return replay
            if row.status == "rejected":
                projected = self.project(row)
                self._record_command(
                    session,
                    tenant_id,
                    user_id,
                    task_id,
                    idempotency_key,
                    "reject",
                    request_hash,
                    projected,
                    now,
                )
                return projected
            if row.status != "pending":
                raise InvalidStateError(f"Pending write status {row.status} cannot be rejected.")
            task = session.scalar(
                select(ResearchTaskRow).where(ResearchTaskRow.task_id == task_id).with_for_update()
            )
            if task is None or task.status != "paused":
                raise InvalidStateError("Task must be paused while rejecting a write.")
            row.status = "rejected"
            row.decision_json = WriteDecision(
                action="rejected", actor_id=user_id, at=now, reason=reason
            ).model_dump(mode="json")
            row.updated_at = now
            self._resume(session, task, write_id, "write_rejected", now)
            self._audit(session, tenant_id, user_id, "pending_write.reject", write_id, "succeeded")
            self._event(session, task, ["pending_write", "task_status"])
            session.flush()
            projected = self.project(row)
            self._record_command(
                session,
                tenant_id,
                user_id,
                task_id,
                idempotency_key,
                "reject",
                request_hash,
                projected,
                now,
            )
            return projected

    @staticmethod
    def _command_replay(
        session: Any,
        tenant_id: str,
        user_id: str,
        task_id: str,
        idempotency_key: str | None,
        request_hash: str,
    ) -> PendingWrite | None:
        if idempotency_key is None:
            return None
        command = session.scalar(
            select(ResearchTaskCommandRow).where(
                ResearchTaskCommandRow.tenant_id == tenant_id,
                ResearchTaskCommandRow.user_id == user_id,
                ResearchTaskCommandRow.task_id == task_id,
                ResearchTaskCommandRow.idempotency_key == idempotency_key,
            )
        )
        if command is None:
            return None
        if command.request_hash != request_hash:
            raise IdempotencyConflictError(
                "Idempotency-Key was used with a different pending-write command."
            )
        return PendingWrite.model_validate(command.response_json)

    @staticmethod
    def _record_command(
        session: Any,
        tenant_id: str,
        user_id: str,
        task_id: str,
        idempotency_key: str | None,
        command_type: str,
        request_hash: str,
        projected: PendingWrite,
        now: datetime,
    ) -> None:
        if idempotency_key is None:
            return
        session.add(
            ResearchTaskCommandRow(
                tenant_id=tenant_id,
                user_id=user_id,
                task_id=task_id,
                idempotency_key=idempotency_key,
                command_type=command_type,
                request_hash=request_hash,
                accepted_http_status=202,
                response_json=projected.model_dump(mode="json"),
                created_at=now,
            )
        )

    @staticmethod
    def _resume(session: Any, task: ResearchTaskRow, write_id: str, position: str, now: datetime) -> None:
        state = ResearchState.model_validate(task.latest_state)
        state.pending_write = None
        state.timestamps.updated_at = now
        payload = state.model_dump(mode="json")
        task.latest_state = payload
        task.status = "pending"
        task.lease_token = None
        task.leased_until = None
        task.budget_accounted_at = None
        task.updated_at = now
        session.add(
            ResearchCheckpointRow(
                checkpoint_id=new_id("ckpt"), tenant_id=task.tenant_id, task_id=task.task_id,
                checkpoint_seq=CheckpointRepository(session, task.tenant_id, task.task_id).next_seq(),
                state_snapshot=payload, graph_position=position,
                scheduling_state={"write_id": write_id},
            )
        )
        session.add(
            OutboxMessageRow(
                message_id=new_id("outbox"), tenant_id=task.tenant_id,
                kind="research.schedule", aggregate_id=task.task_id,
                payload_json={"task_id": task.task_id},
                idempotency_key=f"write:{write_id}:{position}:resume", available_at=now,
            )
        )

    @staticmethod
    def _audit(session: Any, tenant_id: str, actor: str, action: str, write_id: str, result: str, reason: str | None = None) -> None:
        session.add(
            AuditLogRow(
                audit_id=new_id("audit"), tenant_id=tenant_id, actor_ref=f"user:{actor}",
                action=action, resource_ref=f"pending_write:{write_id}", result=result,
                reason_code=reason,
            )
        )

    @staticmethod
    def _event(session: Any, task: ResearchTaskRow, changed: list[str]) -> None:
        event = ResearchEventRow(
            tenant_id=task.tenant_id, task_id=task.task_id, event_type="task_updated",
            payload_json={"event_id": 0, "task_id": task.task_id, "changed": changed, "at": utcnow().isoformat()},
        )
        session.add(event)
        session.flush()
        event.payload_json = {**event.payload_json, "event_id": event.event_id}
        session.add(
            OutboxMessageRow(
                message_id=new_id("outbox"), tenant_id=task.tenant_id,
                kind="research.event.publish", aggregate_id=str(event.event_id),
                payload_json={"task_id": task.task_id, "event_id": event.event_id},
                idempotency_key=f"event:{event.event_id}", available_at=utcnow(),
            )
        )


class WriteExecutor:
    def __init__(
        self,
        session_factory: SessionFactory,
        policies: WritePolicyService,
        credentials: CredentialResolver,
        targets: WriteTargetFactory | None = None,
        egress_policy: DatabaseEgressPolicy | None = None,
    ) -> None:
        self.session_factory = session_factory
        self.policies = policies
        self.credentials = credentials
        self.targets = targets or PostgresWriteTargetFactory()
        self.egress_policy = egress_policy
        self.compiler = SqlCompiler()

    def execute(self, write_id: str, plan_hash: str) -> WriteResult:
        with self.session_factory() as session, session.begin():
            row = session.scalar(
                select(PendingWriteRow).where(PendingWriteRow.write_id == write_id).with_for_update()
            )
            if row is None:
                raise NotFoundError("Pending write was not found.")
            if row.plan_hash != plan_hash:
                raise PlanHashMismatchError("Executor plan hash mismatch.")
            if row.status in {"executed", "failed", "stale"} and row.result_json:
                return WriteResult.model_validate(row.result_json)
            if row.status not in {"approved", "executing"}:
                raise InvalidStateError(f"Pending write status {row.status} is not executable.")
            row.status = "executing"
            row.updated_at = utcnow()
            pending = PendingWriteService.project(row)

        try:
            policy = self.policies.resolve(
                tenant_id=pending.tenant_id,
                user_id=pending.user_id,
                task_id=pending.task_id,
                plan=pending.plan,
            )
            if policy.revision != pending.policy_revision:
                raise StaleWritePlanError("policy_revision_changed")
            if self.egress_policy is not None:
                self.egress_policy.validate(policy.datasource)
            # Credential resolution deliberately happens only after current authorization succeeds.
            credential = resolve_database_credential(
                self.credentials,
                tenant_id=pending.tenant_id,
                credential_ref=policy.datasource.credential_ref,
                mode="write",
            )
            compiled = self.compiler.compile(
                pending.plan, schema_name=policy.table.schema_name
            )
            result = self.targets.create(policy.datasource, credential).execute(
                compiled=compiled,
                write_id=pending.write_id,
                operation_key=pending.operation_key,
                plan_hash=pending.plan_hash,
                max_write_rows=policy.table.max_write_rows,
                has_preconditions=bool(pending.plan.preconditions),
            )
        except StaleWritePlanError:
            result = WriteResult(
                status="stale", affected_rows=0, operation_key=pending.operation_key,
                error_code="stale_write_plan",
            )
        except WriteLimitExceededError:
            result = WriteResult(
                status="failed", affected_rows=0, operation_key=pending.operation_key,
                error_code="write_limit_exceeded",
            )
        except IdempotencyLedgerConflictError:
            result = WriteResult(
                status="failed", affected_rows=0, operation_key=pending.operation_key,
                error_code="idempotency_conflict",
            )
        except DatabaseWriteFailedError:
            result = WriteResult(
                status="failed", affected_rows=0, operation_key=pending.operation_key,
                error_code="database_write_failed",
            )
        except PolicyDeniedError:
            result = WriteResult(
                status="stale", affected_rows=0, operation_key=pending.operation_key,
                error_code="stale_write_plan",
            )
        except PermissionError:
            result = WriteResult(
                status="failed", affected_rows=0, operation_key=pending.operation_key,
                error_code="write_credential_unavailable",
            )

        with self.session_factory() as session, session.begin():
            row = session.scalar(
                select(PendingWriteRow)
                .where(
                    PendingWriteRow.tenant_id == pending.tenant_id,
                    PendingWriteRow.write_id == write_id,
                )
                .with_for_update()
            )
            assert row is not None
            if row.result_json is not None:
                return WriteResult.model_validate(row.result_json)
            row.status = result.status
            row.result_json = result.model_dump(mode="json")
            row.updated_at = utcnow()
            task = session.scalar(
                select(ResearchTaskRow).where(ResearchTaskRow.task_id == row.task_id).with_for_update()
            )
            assert task is not None
            position = "write_stale" if result.status == "stale" else "write_terminal"
            PendingWriteService._resume(session, task, write_id, position, utcnow())
            session.add(
                AuditLogRow(
                    audit_id=new_id("audit"), tenant_id=row.tenant_id,
                    actor_ref="system:write_executor", action="pending_write.execute",
                    resource_ref=f"pending_write:{write_id}",
                    result="succeeded" if result.status == "executed" else "failed",
                    reason_code=result.error_code,
                )
            )
            PendingWriteService._event(session, task, ["pending_write", "task_status"])
            return result
