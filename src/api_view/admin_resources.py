from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any, Literal, Protocol

from sqlalchemy import delete, select

from src.agent.tools.database_read import DataSourceService, resolve_database_credential
from src.agent.schema.model_config import BindingKey, FrozenModelBinding, ResolvedModelRevision
from src.agent.backends.persistence.database import SessionFactory
from src.agent.backends.persistence.models import (
    AuditLogRow,
    DataSourceRow,
    DataSourceTablePolicyRow,
    DataSourceUserGrantRow,
    ModelBindingRow,
    ModelConfigHeadRow,
    ModelConfigRevocationRow,
    ModelConfigRow,
    TenantUserMembershipRow,
    utcnow,
)
from src.agent.errors import (
    ConflictError,
    DependencyUnavailableError,
    InvalidArgumentError,
    NotFoundError,
    PolicyDeniedError,
)


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def _audit(
    session: Any,
    *,
    tenant_id: str,
    user_id: str,
    action: str,
    resource_ref: str,
) -> None:
    session.add(
        AuditLogRow(
            audit_id=_new_id("audit"),
            tenant_id=tenant_id,
            actor_ref=f"user:{user_id}",
            action=action,
            resource_ref=resource_ref,
            result="succeeded",
        )
    )


@dataclass(frozen=True)
class DataSourceCreate:
    name: str
    host: str
    port: int
    database_name: str
    ssl_mode: str
    credential_ref: str


@dataclass(frozen=True)
class DataSourcePatch:
    name: str | None = None
    host: str | None = None
    port: int | None = None
    database_name: str | None = None
    ssl_mode: str | None = None
    credential_ref: str | None = None


@dataclass(frozen=True)
class TablePolicyInput:
    schema_name: str
    table_name: str
    readable_columns: list[str]
    writable_columns: list[str]
    allow_insert: bool
    allow_update: bool
    allow_delete: bool
    max_write_rows: int


class DataSourceAdminService:
    """Tenant-admin lifecycle over the Phase 6 DataSource source of truth."""

    def __init__(self, session_factory: SessionFactory, execution: DataSourceService) -> None:
        self.session_factory = session_factory
        self.execution = execution

    def create(
        self, *, tenant_id: str, user_id: str, command: DataSourceCreate
    ) -> DataSourceRow:
        now = utcnow()
        row = DataSourceRow(
            datasource_id=_new_id("ds"),
            tenant_id=tenant_id,
            name=command.name,
            host=command.host,
            port=command.port,
            database_name=command.database_name,
            ssl_mode=command.ssl_mode,
            credential_ref=command.credential_ref,
            enabled=False,
            created_at=now,
            updated_at=now,
        )
        with self.session_factory() as session, session.begin():
            existing = session.scalar(
                select(DataSourceRow).where(
                    DataSourceRow.tenant_id == tenant_id,
                    DataSourceRow.name == command.name,
                )
            )
            if existing is not None:
                raise ConflictError("A DataSource with this name already exists.")
            session.add(row)
            _audit(
                session,
                tenant_id=tenant_id,
                user_id=user_id,
                action="datasource.create",
                resource_ref=f"datasource:{row.datasource_id}",
            )
        return row

    def update(
        self,
        *,
        tenant_id: str,
        user_id: str,
        datasource_id: str,
        command: DataSourcePatch,
    ) -> DataSourceRow:
        changes = {
            name: value
            for name, value in command.__dict__.items()
            if value is not None
        }
        if not changes:
            raise InvalidArgumentError("At least one DataSource field must be supplied.")
        with self.session_factory() as session, session.begin():
            row = self._get(session, tenant_id, datasource_id, for_update=True)
            for name, value in changes.items():
                setattr(row, name, value)
            row.updated_at = utcnow()
            _audit(
                session,
                tenant_id=tenant_id,
                user_id=user_id,
                action="datasource.update",
                resource_ref=f"datasource:{datasource_id}",
            )
            session.flush()
            session.expunge(row)
        return row

    def test(self, *, tenant_id: str, datasource_id: str) -> None:
        with self.session_factory() as session:
            row = self._get(session, tenant_id, datasource_id)
            session.expunge(row)
        try:
            if self.execution.egress_policy is not None:
                self.execution.egress_policy.validate(row)
            credential = resolve_database_credential(
                self.execution.credentials,
                tenant_id=tenant_id,
                credential_ref=row.credential_ref,
                mode="read",
            )
            database = self.execution.databases.get(row, credential, "read")
            database.execute_readonly("SELECT 1", row_limit=1, timeout_ms=5_000)
        except PermissionError as exc:
            raise PolicyDeniedError("DataSource connectivity is denied by policy.") from exc
        except Exception as exc:
            raise DependencyUnavailableError("DataSource connectivity test failed.") from exc

    def set_enabled(
        self,
        *,
        tenant_id: str,
        user_id: str,
        datasource_id: str,
        enabled: bool,
    ) -> DataSourceRow:
        if enabled:
            self.test(tenant_id=tenant_id, datasource_id=datasource_id)
        with self.session_factory() as session, session.begin():
            row = self._get(session, tenant_id, datasource_id, for_update=True)
            row.enabled = enabled
            row.updated_at = utcnow()
            _audit(
                session,
                tenant_id=tenant_id,
                user_id=user_id,
                action=f"datasource.{'enable' if enabled else 'disable'}",
                resource_ref=f"datasource:{datasource_id}",
            )
            session.flush()
            session.expunge(row)
        return row

    def replace_policies(
        self,
        *,
        tenant_id: str,
        user_id: str,
        datasource_id: str,
        policies: list[TablePolicyInput],
    ) -> None:
        keys = [(item.schema_name, item.table_name) for item in policies]
        if len(keys) != len(set(keys)):
            raise InvalidArgumentError("DataSource policy table identities must be unique.")
        with self.session_factory() as session, session.begin():
            self._get(session, tenant_id, datasource_id, for_update=True)
            session.execute(
                delete(DataSourceTablePolicyRow).where(
                    DataSourceTablePolicyRow.tenant_id == tenant_id,
                    DataSourceTablePolicyRow.datasource_id == datasource_id,
                )
            )
            for item in policies:
                session.add(
                    DataSourceTablePolicyRow(
                        tenant_id=tenant_id,
                        datasource_id=datasource_id,
                        **item.__dict__,
                    )
                )
            _audit(
                session,
                tenant_id=tenant_id,
                user_id=user_id,
                action="datasource.policies.replace",
                resource_ref=f"datasource:{datasource_id}",
            )

    def put_grant(
        self,
        *,
        tenant_id: str,
        actor_user_id: str,
        datasource_id: str,
        grantee_user_id: str,
        access_level: Literal["read", "write"],
    ) -> None:
        with self.session_factory() as session, session.begin():
            self._get(session, tenant_id, datasource_id, for_update=True)
            membership = session.get(
                TenantUserMembershipRow, (tenant_id, grantee_user_id)
            )
            if membership is None or not membership.active:
                raise NotFoundError("Tenant user was not found.")
            grant = session.get(
                DataSourceUserGrantRow,
                (tenant_id, datasource_id, grantee_user_id),
            )
            if grant is None:
                session.add(
                    DataSourceUserGrantRow(
                        tenant_id=tenant_id,
                        datasource_id=datasource_id,
                        user_id=grantee_user_id,
                        access_level=access_level,
                    )
                )
            else:
                grant.access_level = access_level
            _audit(
                session,
                tenant_id=tenant_id,
                user_id=actor_user_id,
                action="datasource.grant.put",
                resource_ref=f"datasource:{datasource_id}:user:{grantee_user_id}",
            )

    def delete_grant(
        self,
        *,
        tenant_id: str,
        actor_user_id: str,
        datasource_id: str,
        grantee_user_id: str,
    ) -> None:
        with self.session_factory() as session, session.begin():
            self._get(session, tenant_id, datasource_id, for_update=True)
            grant = session.get(
                DataSourceUserGrantRow,
                (tenant_id, datasource_id, grantee_user_id),
            )
            if grant is None:
                raise NotFoundError("DataSource grant was not found.")
            session.delete(grant)
            _audit(
                session,
                tenant_id=tenant_id,
                user_id=actor_user_id,
                action="datasource.grant.delete",
                resource_ref=f"datasource:{datasource_id}:user:{grantee_user_id}",
            )

    @staticmethod
    def _get(
        session: Any,
        tenant_id: str,
        datasource_id: str,
        *,
        for_update: bool = False,
    ) -> DataSourceRow:
        statement = select(DataSourceRow).where(
            DataSourceRow.tenant_id == tenant_id,
            DataSourceRow.datasource_id == datasource_id,
        )
        if for_update:
            statement = statement.with_for_update()
        row = session.scalar(statement)
        if row is None:
            raise NotFoundError("DataSource was not found.")
        return row


@dataclass(frozen=True)
class ModelConfigCreate:
    provider: str
    model: str
    base_url: str | None
    purpose: Literal["chat", "embedding", "rerank"]
    secret_ref: str | None
    model_config_id: str | None = None


@dataclass(frozen=True)
class ModelConfigPatch:
    provider: str | None = None
    model: str | None = None
    base_url: str | None = None
    purpose: Literal["chat", "embedding", "rerank"] | None = None
    secret_ref: str | None = None


class ModelConfigAdminService:
    """Versioned management over the frozen ModelConfig tables."""

    def __init__(self, session_factory: SessionFactory) -> None:
        self.session_factory = session_factory

    def create(
        self, *, tenant_id: str, user_id: str, command: ModelConfigCreate
    ) -> tuple[ModelConfigRow, ModelConfigHeadRow]:
        model_config_id = command.model_config_id or _new_id("model")
        now = utcnow()
        row = ModelConfigRow(
            model_config_id=model_config_id,
            version=1,
            tenant_id=tenant_id,
            user_id=user_id,
            provider=command.provider,
            model=command.model,
            base_url=command.base_url,
            purpose=command.purpose,
            secret_ref=command.secret_ref,
            created_at=now,
        )
        head = ModelConfigHeadRow(
            tenant_id=tenant_id,
            model_config_id=model_config_id,
            user_id=user_id,
            current_version=1,
            enabled_for_new_tasks=True,
            updated_at=now,
        )
        with self.session_factory() as session, session.begin():
            if session.get(ModelConfigHeadRow, (tenant_id, model_config_id)) is not None:
                raise ConflictError("ModelConfig already exists.")
            session.add_all([row, head])
            _audit(
                session,
                tenant_id=tenant_id,
                user_id=user_id,
                action="model_config.create",
                resource_ref=f"model_config:{model_config_id}:1",
            )
        return row, head

    def update(
        self,
        *,
        tenant_id: str,
        user_id: str,
        model_config_id: str,
        command: ModelConfigPatch,
    ) -> tuple[ModelConfigRow, ModelConfigHeadRow]:
        supplied = {
            key: value
            for key, value in command.__dict__.items()
            if value is not None
        }
        if not supplied:
            raise InvalidArgumentError("At least one ModelConfig field must be supplied.")
        with self.session_factory() as session, session.begin():
            head = self._head(session, tenant_id, model_config_id, for_update=True)
            current = self._revision(
                session, tenant_id, model_config_id, head.current_version
            )
            values = {
                "provider": current.provider,
                "model": current.model,
                "base_url": current.base_url,
                "purpose": current.purpose,
                "secret_ref": current.secret_ref,
            }
            values.update(supplied)
            version = head.current_version + 1
            row = ModelConfigRow(
                model_config_id=model_config_id,
                version=version,
                tenant_id=tenant_id,
                user_id=head.user_id,
                created_at=utcnow(),
                **values,
            )
            session.add(row)
            session.flush()
            head.current_version = version
            head.enabled_for_new_tasks = True
            head.updated_at = utcnow()
            _audit(
                session,
                tenant_id=tenant_id,
                user_id=user_id,
                action="model_config.update",
                resource_ref=f"model_config:{model_config_id}:{version}",
            )
            session.flush()
            session.expunge(row)
            session.expunge(head)
        return row, head

    def disable(
        self, *, tenant_id: str, user_id: str, model_config_id: str
    ) -> ModelConfigHeadRow:
        with self.session_factory() as session, session.begin():
            head = self._head(session, tenant_id, model_config_id, for_update=True)
            head.enabled_for_new_tasks = False
            head.updated_at = utcnow()
            _audit(
                session,
                tenant_id=tenant_id,
                user_id=user_id,
                action="model_config.disable",
                resource_ref=f"model_config:{model_config_id}",
            )
            session.flush()
            session.expunge(head)
        return head

    def revoke(
        self,
        *,
        tenant_id: str,
        user_id: str,
        model_config_id: str,
        version: int,
        reason: str,
    ) -> ModelConfigRevocationRow:
        with self.session_factory() as session, session.begin():
            revision = self._revision(session, tenant_id, model_config_id, version)
            existing = session.get(ModelConfigRevocationRow, (model_config_id, version))
            if existing is not None:
                if existing.tenant_id != tenant_id:
                    raise NotFoundError("ModelConfig revision was not found.")
                return existing
            row = ModelConfigRevocationRow(
                model_config_id=model_config_id,
                version=version,
                tenant_id=tenant_id,
                user_id=revision.user_id,
                revoked_at=utcnow(),
                reason=reason,
            )
            session.add(row)
            _audit(
                session,
                tenant_id=tenant_id,
                user_id=user_id,
                action="model_config.revoke",
                resource_ref=f"model_config:{model_config_id}:{version}",
            )
        return row

    def replace_binding(
        self,
        *,
        tenant_id: str,
        user_id: str,
        binding_key: BindingKey,
        model_config_ids: list[str],
    ) -> None:
        if not model_config_ids or len(model_config_ids) != len(set(model_config_ids)):
            raise InvalidArgumentError("Binding models must be non-empty and unique.")
        expected_purpose = (
            "embedding"
            if binding_key == "embedding"
            else "rerank"
            if binding_key == "rerank"
            else "chat"
        )
        with self.session_factory() as session, session.begin():
            for model_config_id in model_config_ids:
                head = self._head(session, tenant_id, model_config_id)
                revision = self._revision(
                    session, tenant_id, model_config_id, head.current_version
                )
                if revision.purpose != expected_purpose:
                    raise InvalidArgumentError(
                        "ModelConfig purpose does not match the binding key."
                    )
            session.execute(
                delete(ModelBindingRow).where(
                    ModelBindingRow.tenant_id == tenant_id,
                    ModelBindingRow.binding_key == binding_key,
                    ModelBindingRow.purpose == expected_purpose,
                )
            )
            for priority, model_config_id in enumerate(model_config_ids):
                session.add(
                    ModelBindingRow(
                        tenant_id=tenant_id,
                        binding_key=binding_key,
                        purpose=expected_purpose,
                        priority=priority,
                        model_config_id=model_config_id,
                    )
                )
            _audit(
                session,
                tenant_id=tenant_id,
                user_id=user_id,
                action="model_binding.replace",
                resource_ref=f"model_binding:{binding_key}",
            )

    @staticmethod
    def _head(
        session: Any,
        tenant_id: str,
        model_config_id: str,
        *,
        for_update: bool = False,
    ) -> ModelConfigHeadRow:
        statement = select(ModelConfigHeadRow).where(
            ModelConfigHeadRow.tenant_id == tenant_id,
            ModelConfigHeadRow.model_config_id == model_config_id,
        )
        if for_update:
            statement = statement.with_for_update()
        head = session.scalar(statement)
        if head is None:
            raise NotFoundError("ModelConfig was not found.")
        return head

    @staticmethod
    def _revision(
        session: Any, tenant_id: str, model_config_id: str, version: int
    ) -> ModelConfigRow:
        row = session.scalar(
            select(ModelConfigRow).where(
                ModelConfigRow.tenant_id == tenant_id,
                ModelConfigRow.model_config_id == model_config_id,
                ModelConfigRow.version == version,
            )
        )
        if row is None:
            raise NotFoundError("ModelConfig revision was not found.")
        return row


class ModelRuntimeForAdmin(Protocol):
    def build_chat_model(
        self, tenant_id: str, binding: FrozenModelBinding
    ) -> Any: ...

    def embed(
        self, tenant_id: str, binding: FrozenModelBinding, texts: list[str]
    ) -> list[list[float]]: ...

    def rerank(
        self,
        tenant_id: str,
        binding: FrozenModelBinding,
        query: str,
        candidates: list[str],
    ) -> list[float]: ...


def test_model_revision(
    *,
    runtime: ModelRuntimeForAdmin,
    tenant_id: str,
    row: ModelConfigRow,
) -> None:
    binding_key: BindingKey = (
        "embedding"
        if row.purpose == "embedding"
        else "rerank"
        if row.purpose == "rerank"
        else "reasoning"
    )
    binding = FrozenModelBinding(
        binding_key=binding_key,
        purpose=row.purpose,  # type: ignore[arg-type]
        primary=ResolvedModelRevision(
            model_config_id=row.model_config_id, version=row.version
        ),
    )
    try:
        if row.purpose == "chat":
            runtime.build_chat_model(tenant_id, binding).invoke_text("Reply with OK.")
        elif row.purpose == "embedding":
            runtime.embed(tenant_id, binding, ["ping"])
        else:
            runtime.rerank(tenant_id, binding, "ping", ["ping"])
    except Exception as exc:
        raise DependencyUnavailableError("ModelConfig test request failed.") from exc
