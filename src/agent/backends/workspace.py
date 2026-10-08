from __future__ import annotations

import hashlib
import io
import posixpath
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import BinaryIO, Protocol

from pydantic import BaseModel, ConfigDict
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from src.agent.identity import sha256_hex
from src.agent.backends.persistence.database import SessionFactory
from src.agent.backends.persistence.models import (
    ExportJobRow,
    ResearchTaskRow,
    ShareRow,
    WorkspaceFileRow,
    utcnow,
)
from src.agent.errors import IdempotencyConflictError, NotFoundError, QuotaExceededError
from src.agent.task_service import new_id


class ObjectStore(Protocol):
    def put_temp(self, data: bytes, *, prefix: str) -> str: ...

    def put_temp_stream(self, stream: BinaryIO, length: int, *, prefix: str) -> str: ...

    def integrity(self, storage_ref: str) -> tuple[int, str]: ...

    def promote(self, temp_ref: str, final_ref: str) -> None: ...

    def get(self, storage_ref: str) -> bytes: ...

    def exists(self, storage_ref: str) -> bool: ...

    def delete(self, storage_ref: str) -> None: ...

    def list_prefix(self, prefix: str) -> list[str]: ...


class InMemoryObjectStore:
    def __init__(self) -> None:
        self._objects: dict[str, bytes] = {}
        self._counter = 0
        self._lock = threading.Lock()

    def put_temp(self, data: bytes, *, prefix: str) -> str:
        with self._lock:
            self._counter += 1
            ref = f"{prefix.rstrip('/')}/tmp/{self._counter:08d}"
            self._objects[ref] = bytes(data)
            return ref

    def put_temp_stream(self, stream: BinaryIO, length: int, *, prefix: str) -> str:
        payload = stream.read(length + 1)
        if len(payload) != length:
            raise ValueError("stream length changed during upload")
        return self.put_temp(payload, prefix=prefix)

    def promote(self, temp_ref: str, final_ref: str) -> None:
        with self._lock:
            if temp_ref not in self._objects:
                if final_ref in self._objects:
                    return
                raise FileNotFoundError(temp_ref)
            self._objects[final_ref] = self._objects[temp_ref]
            del self._objects[temp_ref]

    def integrity(self, storage_ref: str) -> tuple[int, str]:
        payload = self.get(storage_ref)
        return len(payload), sha256_hex(payload)

    def get(self, storage_ref: str) -> bytes:
        try:
            return self._objects[storage_ref]
        except KeyError as exc:
            raise FileNotFoundError(storage_ref) from exc

    def exists(self, storage_ref: str) -> bool:
        return storage_ref in self._objects

    def delete(self, storage_ref: str) -> None:
        self._objects.pop(storage_ref, None)

    def list_prefix(self, prefix: str) -> list[str]:
        return sorted(ref for ref in self._objects if ref.startswith(prefix))


class MinioObjectStore:
    """Private-bucket adapter. Physical object names never cross this boundary."""

    def __init__(
        self,
        endpoint: str,
        access_key: str,
        secret_key: str,
        *,
        bucket: str = "workspaces",
        secure: bool = False,
    ) -> None:
        try:
            from minio import Minio
        except ImportError as exc:  # pragma: no cover - depends on deployment extra
            raise RuntimeError("minio package is required for MinioObjectStore") from exc
        self.client = Minio(
            endpoint,
            access_key=access_key,
            secret_key=secret_key,
            secure=secure,
        )
        self.bucket = bucket
        if not self.client.bucket_exists(bucket):
            self.client.make_bucket(bucket)

    def put_temp(self, data: bytes, *, prefix: str) -> str:
        ref = f"{prefix.rstrip('/')}/tmp/{new_id('obj')}"
        self.client.put_object(self.bucket, ref, io.BytesIO(data), length=len(data))
        return ref

    def put_temp_stream(self, stream: BinaryIO, length: int, *, prefix: str) -> str:
        ref = f"{prefix.rstrip('/')}/tmp/{new_id('obj')}"
        self.client.put_object(self.bucket, ref, stream, length=length)
        return ref

    def promote(self, temp_ref: str, final_ref: str) -> None:
        from minio.commonconfig import CopySource

        if self.exists(final_ref):
            self.delete(temp_ref)
            return
        self.client.copy_object(self.bucket, final_ref, CopySource(self.bucket, temp_ref))
        self.client.remove_object(self.bucket, temp_ref)

    def integrity(self, storage_ref: str) -> tuple[int, str]:
        response = self.client.get_object(self.bucket, storage_ref)
        digest = hashlib.sha256()
        size = 0
        try:
            while chunk := response.read(1024 * 1024):
                size += len(chunk)
                digest.update(chunk)
        finally:
            response.close()
            response.release_conn()
        return size, digest.hexdigest()

    def get(self, storage_ref: str) -> bytes:
        response = self.client.get_object(self.bucket, storage_ref)
        try:
            return response.read()
        finally:
            response.close()
            response.release_conn()

    def exists(self, storage_ref: str) -> bool:
        try:
            self.client.stat_object(self.bucket, storage_ref)
            return True
        except Exception as exc:  # MinIO exposes S3Error only at runtime
            if getattr(exc, "code", None) in {"NoSuchKey", "NoSuchObject"}:
                return False
            raise

    def delete(self, storage_ref: str) -> None:
        self.client.remove_object(self.bucket, storage_ref)

    def list_prefix(self, prefix: str) -> list[str]:
        return [
            item.object_name
            for item in self.client.list_objects(
                self.bucket, prefix=prefix, recursive=True
            )
        ]


class WorkspaceFile(BaseModel):
    model_config = ConfigDict(extra="forbid")
    workspace_path: str
    content_hash: str
    mime_type: str
    size: int
    status: str
    producer_tool_call_id: str | None = None


@dataclass(frozen=True)
class WorkspaceLimits:
    tenant_bytes: int = 20 * 1024**3
    task_bytes: int = 1 * 1024**3
    file_bytes: int = 256 * 1024**2


def normalize_workspace_path(path: str) -> str:
    candidate = path.replace("\\", "/").strip()
    normalized = posixpath.normpath(candidate)
    if (
        not candidate
        or candidate.startswith("/")
        or normalized in {"", ".", ".."}
        or normalized.startswith("../")
        or ":" in normalized.split("/", 1)[0]
    ):
        raise ValueError("workspace_path must be a task-relative safe path")
    return normalized


class WorkspaceService:
    def __init__(
        self,
        session_factory: SessionFactory,
        object_store: ObjectStore,
        *,
        limits: WorkspaceLimits | None = None,
    ) -> None:
        self.session_factory = session_factory
        self.object_store = object_store
        self.limits = limits or WorkspaceLimits()

    def stage_bytes(self, tenant_id: str, task_id: str, data: bytes) -> str:
        self._assert_task(tenant_id, task_id)
        return self.object_store.put_temp(data, prefix=f"{tenant_id}/{task_id}")

    def publish_bytes(
        self,
        *,
        tenant_id: str,
        task_id: str,
        path: str,
        data: bytes,
        mime_type: str,
        operation_key: str,
        producer_tool_call_id: str | None = None,
    ) -> WorkspaceFile:
        temp_ref = self.stage_bytes(tenant_id, task_id, data)
        return self.finalize_file(
            temp_ref,
            tenant_id=tenant_id,
            task_id=task_id,
            path=path,
            content_hash=sha256_hex(data),
            size=len(data),
            mime_type=mime_type,
            operation_key=operation_key,
            producer_tool_call_id=producer_tool_call_id,
        )

    def publish_stream(
        self,
        *,
        tenant_id: str,
        task_id: str,
        path: str,
        stream: BinaryIO,
        size: int,
        content_hash: str,
        mime_type: str,
        operation_key: str,
        producer_tool_call_id: str | None = None,
    ) -> WorkspaceFile:
        self._assert_task(tenant_id, task_id)
        temp_ref = self.object_store.put_temp_stream(
            stream, size, prefix=f"{tenant_id}/{task_id}"
        )
        return self.finalize_file(
            temp_ref,
            tenant_id=tenant_id,
            task_id=task_id,
            path=path,
            content_hash=content_hash,
            size=size,
            mime_type=mime_type,
            operation_key=operation_key,
            producer_tool_call_id=producer_tool_call_id,
        )

    def finalize_file(
        self,
        temp_object: str,
        *,
        tenant_id: str,
        task_id: str,
        path: str,
        content_hash: str,
        size: int,
        mime_type: str,
        operation_key: str,
        producer_tool_call_id: str | None = None,
    ) -> WorkspaceFile:
        workspace_path = normalize_workspace_path(path)
        if size < 0 or size > self.limits.file_bytes:
            self.object_store.delete(temp_object)
            raise QuotaExceededError("Workspace file exceeds the single-file quota.")
        stored_size, stored_hash = self.object_store.integrity(temp_object)
        if stored_size != size or stored_hash != content_hash:
            self.object_store.delete(temp_object)
            raise IdempotencyConflictError("Workspace content metadata does not match its bytes.")
        final_ref = f"{tenant_id}/{task_id}/{workspace_path}"

        try:
            with self.session_factory() as session, session.begin():
                self._assert_task_in_session(session, tenant_id, task_id)
                existing = session.scalar(
                    select(WorkspaceFileRow).where(
                        WorkspaceFileRow.tenant_id == tenant_id,
                        WorkspaceFileRow.task_id == task_id,
                        WorkspaceFileRow.operation_key == operation_key,
                    )
                )
                if existing is not None:
                    if (
                        existing.workspace_path != workspace_path
                        or existing.content_hash != content_hash
                        or existing.size != size
                        or existing.mime_type != mime_type
                    ):
                        raise IdempotencyConflictError(
                            "Workspace operation was replayed with different content."
                        )
                    if existing.status == "ready" and existing.storage_ref:
                        self.object_store.delete(temp_object)
                        return self._public(existing)
                    row = existing
                else:
                    self._check_quotas(session, tenant_id, task_id, size)
                    row = WorkspaceFileRow(
                        workspace_file_id=new_id("wsf"),
                        tenant_id=tenant_id,
                        task_id=task_id,
                        workspace_path=workspace_path,
                        storage_ref=final_ref,
                        content_hash=content_hash,
                        mime_type=mime_type,
                        size=size,
                        status="pending",
                        operation_key=operation_key,
                        producer_tool_call_id=producer_tool_call_id,
                    )
                    session.add(row)
                    session.flush()
        except IntegrityError as exc:
            self.object_store.delete(temp_object)
            raise IdempotencyConflictError("Workspace path is already published.") from exc
        except Exception:
            self.object_store.delete(temp_object)
            raise

        try:
            self.object_store.promote(temp_object, final_ref)
            with self.session_factory() as session, session.begin():
                promoted_row = session.scalar(
                    select(WorkspaceFileRow)
                    .where(
                        WorkspaceFileRow.tenant_id == tenant_id,
                        WorkspaceFileRow.task_id == task_id,
                        WorkspaceFileRow.operation_key == operation_key,
                    )
                    .with_for_update()
                )
                if promoted_row is None:
                    raise RuntimeError("workspace reservation disappeared")
                if promoted_row.content_hash != content_hash:
                    raise IdempotencyConflictError("Workspace reservation hash changed.")
                promoted_row.status = "ready"
                promoted_row.storage_ref = final_ref
                promoted_row.updated_at = utcnow()
                session.flush()
                return self._public(promoted_row)
        except Exception:
            self.object_store.delete(temp_object)
            with self.session_factory() as session, session.begin():
                failed_row = session.scalar(
                    select(WorkspaceFileRow).where(
                        WorkspaceFileRow.tenant_id == tenant_id,
                        WorkspaceFileRow.task_id == task_id,
                        WorkspaceFileRow.operation_key == operation_key,
                    )
                )
                if failed_row is not None and failed_row.status != "ready":
                    failed_row.status = "failed"
                    failed_row.updated_at = utcnow()
            raise

    def metadata(self, tenant_id: str, task_id: str, path: str) -> WorkspaceFile:
        row = self._ready_row(tenant_id, task_id, path)
        return self._public(row)

    def read(self, tenant_id: str, task_id: str, path: str) -> bytes:
        row = self._ready_row(tenant_id, task_id, path)
        assert row.storage_ref is not None
        payload = self.object_store.get(row.storage_ref)
        if len(payload) != row.size or sha256_hex(payload) != row.content_hash:
            raise RuntimeError("Workspace object integrity check failed")
        return payload

    def list(self, tenant_id: str, task_id: str, prefix: str = "") -> list[WorkspaceFile]:
        self._assert_task(tenant_id, task_id)
        safe_prefix = "" if not prefix else normalize_workspace_path(prefix)
        with self.session_factory() as session:
            rows = session.scalars(
                select(WorkspaceFileRow)
                .where(
                    WorkspaceFileRow.tenant_id == tenant_id,
                    WorkspaceFileRow.task_id == task_id,
                    WorkspaceFileRow.status == "ready",
                    WorkspaceFileRow.workspace_path.startswith(safe_prefix),
                )
                .order_by(WorkspaceFileRow.workspace_path)
            ).all()
            return [self._public(row) for row in rows]

    def reconcile(
        self,
        tenant_id: str,
        task_id: str,
        *,
        now: datetime | None = None,
        orphan_grace_seconds: int = 3600,
    ) -> dict[str, int]:
        """Converge incomplete metadata and remove unreferenced task temp objects."""
        ready = failed = missing = removed_temp = removed_orphan = 0
        now = now or utcnow()
        cutoff = now - timedelta(seconds=max(0, orphan_grace_seconds))
        orphan_refs: list[str] = []
        with self.session_factory() as session, session.begin():
            self._assert_task_in_session(session, tenant_id, task_id)
            rows = session.scalars(
                select(WorkspaceFileRow).where(
                    WorkspaceFileRow.tenant_id == tenant_id,
                    WorkspaceFileRow.task_id == task_id,
                    WorkspaceFileRow.status.in_(["pending", "failed"]),
                )
            ).all()
            for row in rows:
                if row.storage_ref and self.object_store.exists(row.storage_ref):
                    row.status = "ready"
                    row.updated_at = utcnow()
                    ready += 1
                elif row.status == "pending":
                    row.status = "failed"
                    row.updated_at = utcnow()
                    failed += 1
            integrity_rows = session.scalars(
                select(WorkspaceFileRow).where(
                    WorkspaceFileRow.tenant_id == tenant_id,
                    WorkspaceFileRow.task_id == task_id,
                    WorkspaceFileRow.status == "ready",
                )
            ).all()
            for row in integrity_rows:
                if row.storage_ref and self.object_store.exists(row.storage_ref):
                    continue
                # Preserve metadata/provenance for operator inspection while making
                # the unreadable object unavailable through normal Workspace reads.
                row.status = "failed"
                row.updated_at = utcnow()
                missing += 1
            ready_rows = session.scalars(
                select(WorkspaceFileRow).where(
                    WorkspaceFileRow.tenant_id == tenant_id,
                    WorkspaceFileRow.task_id == task_id,
                    WorkspaceFileRow.status == "ready",
                    WorkspaceFileRow.updated_at <= cutoff,
                    WorkspaceFileRow.workspace_path.startswith("reports/"),
                )
            ).all()
            for row in ready_rows:
                if not self._is_collectable_delivery_file(session, row, now):
                    continue
                if row.storage_ref:
                    orphan_refs.append(row.storage_ref)
                session.delete(row)
                removed_orphan += 1
        for ref in self.object_store.list_prefix(f"{tenant_id}/{task_id}/tmp/"):
            self.object_store.delete(ref)
            removed_temp += 1
        for ref in orphan_refs:
            self.object_store.delete(ref)
        return {
            "ready": ready,
            "failed": failed,
            "missing": missing,
            "removed_temp": removed_temp,
            "removed_orphan": removed_orphan,
        }

    @staticmethod
    def _is_collectable_delivery_file(
        session: object, row: WorkspaceFileRow, now: datetime
    ) -> bool:
        parts = row.workspace_path.split("/")
        if len(parts) >= 3 and parts[:2] == ["reports", "shares"]:
            share = session.get(ShareRow, parts[2])  # type: ignore[attr-defined]
            if share is None:
                return True
            expires_at = share.expires_at
            if expires_at.tzinfo is None:
                expires_at = expires_at.replace(tzinfo=timezone.utc)
            return share.revoked_at is not None or expires_at <= now
        if len(parts) == 3 and parts[:2] == ["reports", "exports"]:
            export_id = parts[2].rsplit(".", 1)[0]
            export = session.get(ExportJobRow, export_id)  # type: ignore[attr-defined]
            if export is None or export.status == "failed":
                return True
            if export.status == "completed" and export.expires_at is not None:
                expires_at = export.expires_at
                if expires_at.tzinfo is None:
                    expires_at = expires_at.replace(tzinfo=timezone.utc)
                return expires_at <= now
        return False

    def _ready_row(self, tenant_id: str, task_id: str, path: str) -> WorkspaceFileRow:
        workspace_path = normalize_workspace_path(path)
        self._assert_task(tenant_id, task_id)
        with self.session_factory() as session:
            row = session.scalar(
                select(WorkspaceFileRow).where(
                    WorkspaceFileRow.tenant_id == tenant_id,
                    WorkspaceFileRow.task_id == task_id,
                    WorkspaceFileRow.workspace_path == workspace_path,
                    WorkspaceFileRow.status == "ready",
                )
            )
            if row is None:
                raise NotFoundError("Workspace file was not found.")
            session.expunge(row)
            return row

    def _assert_task(self, tenant_id: str, task_id: str) -> None:
        with self.session_factory() as session:
            self._assert_task_in_session(session, tenant_id, task_id)

    @staticmethod
    def _assert_task_in_session(session: object, tenant_id: str, task_id: str) -> None:
        row = session.scalar(  # type: ignore[attr-defined]
            select(ResearchTaskRow).where(
                ResearchTaskRow.tenant_id == tenant_id,
                ResearchTaskRow.task_id == task_id,
                ResearchTaskRow.deleted_at.is_(None),
            )
        )
        if row is None:
            raise NotFoundError("Task was not found.")

    def _check_quotas(self, session: object, tenant_id: str, task_id: str, size: int) -> None:
        counted = ["pending", "ready"]
        task_total = session.scalar(  # type: ignore[attr-defined]
            select(func.coalesce(func.sum(WorkspaceFileRow.size), 0)).where(
                WorkspaceFileRow.tenant_id == tenant_id,
                WorkspaceFileRow.task_id == task_id,
                WorkspaceFileRow.status.in_(counted),
            )
        )
        tenant_total = session.scalar(  # type: ignore[attr-defined]
            select(func.coalesce(func.sum(WorkspaceFileRow.size), 0)).where(
                WorkspaceFileRow.tenant_id == tenant_id,
                WorkspaceFileRow.status.in_(counted),
            )
        )
        if int(task_total or 0) + size > self.limits.task_bytes:
            raise QuotaExceededError("Task workspace quota exceeded.")
        if int(tenant_total or 0) + size > self.limits.tenant_bytes:
            raise QuotaExceededError("Tenant workspace quota exceeded.")

    @staticmethod
    def _public(row: WorkspaceFileRow) -> WorkspaceFile:
        return WorkspaceFile(
            workspace_path=row.workspace_path,
            content_hash=row.content_hash,
            mime_type=row.mime_type,
            size=row.size,
            status=row.status,
            producer_tool_call_id=row.producer_tool_call_id,
        )
