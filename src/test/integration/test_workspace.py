from __future__ import annotations

import pytest
from sqlalchemy import select

from src.agent.errors import IdempotencyConflictError, NotFoundError, QuotaExceededError
from src.agent.backends.persistence.models import WorkspaceFileRow
from src.agent.task_service import CreateTaskRequest
from src.agent.backends.workspace import InMemoryObjectStore, WorkspaceLimits, WorkspaceService


def task(container, key="workspace"):
    return container.task_service.create(
        "tenant_test", "user_test", key, CreateTaskRequest(query="q")
    ).projection.task_id


def test_workspace_publish_replay_integrity_and_isolation(make_container) -> None:
    container, _ = make_container({})
    task_id = task(container)
    first = container.workspace_service.publish_bytes(
        tenant_id="tenant_test",
        task_id=task_id,
        path="files/result.txt",
        data=b"result",
        mime_type="text/plain",
        operation_key="a" * 64,
    )
    replay = container.workspace_service.publish_bytes(
        tenant_id="tenant_test",
        task_id=task_id,
        path="files/result.txt",
        data=b"result",
        mime_type="text/plain",
        operation_key="a" * 64,
    )
    assert first == replay
    assert container.workspace_service.read("tenant_test", task_id, first.workspace_path) == b"result"
    assert "storage_ref" not in first.model_dump()
    with pytest.raises(IdempotencyConflictError):
        container.workspace_service.publish_bytes(
            tenant_id="tenant_test",
            task_id=task_id,
            path="files/result.txt",
            data=b"different",
            mime_type="text/plain",
            operation_key="a" * 64,
        )
    with pytest.raises(NotFoundError):
        container.workspace_service.read("other", task_id, first.workspace_path)
    with pytest.raises(ValueError):
        container.workspace_service.read("tenant_test", task_id, "../escape")


def test_workspace_quotas_and_reconciler(make_container, session_factory) -> None:
    container, _ = make_container({})
    task_id = task(container, "workspace-quota")
    store = InMemoryObjectStore()
    service = WorkspaceService(
        session_factory,
        store,
        limits=WorkspaceLimits(tenant_bytes=8, task_bytes=8, file_bytes=4),
    )
    with pytest.raises(QuotaExceededError):
        service.publish_bytes(
            tenant_id="tenant_test",
            task_id=task_id,
            path="files/large",
            data=b"12345",
            mime_type="application/octet-stream",
            operation_key="b" * 64,
        )
    temp = service.stage_bytes("tenant_test", task_id, b"orphan")
    assert store.exists(temp)
    outcome = service.reconcile("tenant_test", task_id)
    assert outcome["removed_temp"] == 1
    assert not store.exists(temp)


def test_workspace_reconciler_marks_missing_ready_object_failed(
    make_container, session_factory
) -> None:
    container, _ = make_container({})
    task_id = task(container, "workspace-missing")
    created = container.workspace_service.publish_bytes(
        tenant_id="tenant_test",
        task_id=task_id,
        path="files/missing.txt",
        data=b"historical",
        mime_type="text/plain",
        operation_key="missing-object",
    )
    with session_factory() as session:
        row = session.scalar(
            select(WorkspaceFileRow).where(
                WorkspaceFileRow.task_id == task_id,
                WorkspaceFileRow.workspace_path == created.workspace_path,
            )
        )
        assert row is not None and row.storage_ref is not None
        storage_ref = row.storage_ref
    container.workspace_service.object_store.delete(storage_ref)

    result = container.workspace_service.reconcile("tenant_test", task_id)

    assert result["missing"] == 1
    with session_factory() as session:
        row = session.scalar(
            select(WorkspaceFileRow).where(WorkspaceFileRow.task_id == task_id)
        )
        assert row is not None and row.status == "failed"
    with pytest.raises(NotFoundError):
        container.workspace_service.read("tenant_test", task_id, created.workspace_path)


def test_workspace_task_and_tenant_quotas_cleanup_temp(make_container, session_factory) -> None:
    container, _ = make_container({})
    first_task = task(container, "workspace-task-limit")
    second_task = task(container, "workspace-tenant-limit")
    store = InMemoryObjectStore()
    task_limited = WorkspaceService(
        session_factory,
        store,
        limits=WorkspaceLimits(tenant_bytes=100, task_bytes=6, file_bytes=10),
    )
    task_limited.publish_bytes(
        tenant_id="tenant_test",
        task_id=first_task,
        path="files/first",
        data=b"1234",
        mime_type="application/octet-stream",
        operation_key="task-first",
    )
    with pytest.raises(QuotaExceededError, match="Task"):
        task_limited.publish_bytes(
            tenant_id="tenant_test",
            task_id=first_task,
            path="files/second",
            data=b"123",
            mime_type="application/octet-stream",
            operation_key="task-second",
        )

    tenant_limited = WorkspaceService(
        session_factory,
        store,
        limits=WorkspaceLimits(tenant_bytes=5, task_bytes=10, file_bytes=10),
    )
    with pytest.raises(QuotaExceededError, match="Tenant"):
        tenant_limited.publish_bytes(
            tenant_id="tenant_test",
            task_id=second_task,
            path="files/tenant",
            data=b"12",
            mime_type="application/octet-stream",
            operation_key="tenant-second",
        )
    assert store.list_prefix("tenant_test/") == [
        f"tenant_test/{first_task}/files/first"
    ]
