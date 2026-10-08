from __future__ import annotations

import io
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pandas as pd
from fastapi.testclient import TestClient

from src.api_view.web_main import create_app
from src.agent.tools.datasets import (
    InspectDatasetAdapter,
    ProfileDatasetAdapter,
    QueryDatasetAdapter,
)
from src.agent.runtime.tool_runtime import ToolExecutionContext
from src.agent.task_service import CreateTaskRequest


class Token:
    def is_cancelled(self):
        return False


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
        operation_key="d" * 64,
    )


def new_task(container):
    return container.task_service.create(
        "tenant_test", "user_test", "dataset", CreateTaskRequest(query="q")
    ).projection.task_id


def publish(container, task_id, path, payload, mime, key):
    return container.workspace_service.publish_bytes(
        tenant_id="tenant_test",
        task_id=task_id,
        path=path,
        data=payload,
        mime_type=mime,
        operation_key=key,
    )


def test_csv_bind_inspect_profile_and_safe_query(make_container) -> None:
    container, _ = make_container({})
    task_id = new_task(container)
    source = publish(
        container,
        task_id,
        "files/data/source.csv",
        b"value,label\n1,a\n2,a\n3,b\n100,z\n",
        "text/csv",
        "1" * 64,
    )
    dataset_ref = container.dataset_service.bind(
        tenant_id="tenant_test",
        task_id=task_id,
        workspace_path=source.workspace_path,
        operation_key="2" * 64,
    )
    row = container.dataset_service.resolve("tenant_test", task_id, dataset_ref)
    assert row.source_workspace_path == source.workspace_path
    assert row.manifest_json["tables"][0]["workspace_path"] != source.workspace_path

    inspected = InspectDatasetAdapter(container.dataset_service).execute(
        context(task_id), {"dataset_ref": dataset_ref}
    )
    assert inspected.data["tables"][0]["row_count"] == 4
    profiled = ProfileDatasetAdapter(container.dataset_service).execute(
        context(task_id), {"dataset_ref": dataset_ref, "columns": ["value"]}
    )
    assert profiled.data["profiles"][0]["outlier_count"] == 1
    queried = QueryDatasetAdapter(
        container.dataset_service, container.workspace_service
    ).execute(
        context(task_id),
        {
            "dataset_ref": dataset_ref,
            "query": "WITH x AS (SELECT * FROM dataset) SELECT label, SUM(value) total FROM x GROUP BY label ORDER BY label",
            "row_limit": 100,
        },
    )
    assert queried.ok
    assert queried.data["columns"] == ["label", "total"]
    denied = QueryDatasetAdapter(
        container.dataset_service, container.workspace_service
    ).execute(
        context(task_id),
        {
            "dataset_ref": dataset_ref,
            "query": "SELECT * FROM read_csv_auto('C:/secret.csv')",
            "row_limit": 100,
        },
    )
    assert denied.error.type == "policy_denied"


def test_xlsx_multi_sheet_and_parquet_binding(make_container) -> None:
    container, _ = make_container({})
    task_id = new_task(container)
    workbook = io.BytesIO()
    with pd.ExcelWriter(workbook, engine="openpyxl") as writer:
        pd.DataFrame({"x": [1, 2]}).to_excel(writer, sheet_name="First Sheet", index=False)
        pd.DataFrame({"y": [3]}).to_excel(writer, sheet_name="Second", index=False)
    xlsx = publish(
        container,
        task_id,
        "files/data/book.xlsx",
        workbook.getvalue(),
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "3" * 64,
    )
    xlsx_ref = container.dataset_service.bind(
        tenant_id="tenant_test",
        task_id=task_id,
        workspace_path=xlsx.workspace_path,
        operation_key="4" * 64,
    )
    tables = container.dataset_service.resolve(
        "tenant_test", task_id, xlsx_ref
    ).manifest_json["tables"]
    assert [table["name"] for table in tables] == ["First_Sheet", "Second"]

    parquet_bytes = io.BytesIO()
    pd.DataFrame({"z": [1]}).to_parquet(parquet_bytes, index=False)
    parquet = publish(
        container,
        task_id,
        "files/data/source.parquet",
        parquet_bytes.getvalue(),
        "application/vnd.apache.parquet",
        "5" * 64,
    )
    parquet_ref = container.dataset_service.bind(
        tenant_id="tenant_test",
        task_id=task_id,
        workspace_path=parquet.workspace_path,
        operation_key="6" * 64,
    )
    table = container.dataset_service.resolve(
        "tenant_test", task_id, parquet_ref
    ).manifest_json["tables"][0]
    assert table["workspace_path"] == parquet.workspace_path

    client = TestClient(create_app(container))
    xlsx_upload = client.post(
        f"/api/v1/tasks/{task_id}/data-files",
        headers={"Idempotency-Key": "xlsx-upload"},
        files={
            "file": (
                "book.xlsx",
                workbook.getvalue(),
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )
        },
    )
    parquet_upload = client.post(
        f"/api/v1/tasks/{task_id}/data-files",
        headers={"Idempotency-Key": "parquet-upload"},
        files={
            "file": (
                "source.parquet",
                parquet_bytes.getvalue(),
                "application/vnd.apache.parquet",
            )
        },
    )
    assert xlsx_upload.status_code == parquet_upload.status_code == 201
    assert xlsx_upload.json()["status"] == parquet_upload.json()["status"] == "ready"


def test_data_file_api_streaming_idempotency_and_catalog_binding(make_container) -> None:
    container, _ = make_container({})
    task_id = new_task(container)
    client = TestClient(create_app(container))
    headers = {"Idempotency-Key": "data-upload"}
    files = {"file": ("input.csv", b"value\n1\n2\n", "text/csv")}
    created = client.post(
        f"/api/v1/tasks/{task_id}/data-files", headers=headers, files=files
    )
    assert created.status_code == 201
    assert created.json()["status"] == "ready"
    assert "storage_ref" not in created.json()
    assert "dataset_ref" not in created.json()

    replay = client.post(
        f"/api/v1/tasks/{task_id}/data-files", headers=headers, files=files
    )
    assert replay.status_code == 200
    assert replay.json() == created.json()
    conflict = client.post(
        f"/api/v1/tasks/{task_id}/data-files",
        headers=headers,
        files={"file": ("input.csv", b"value\n3\n", "text/csv")},
    )
    assert conflict.status_code == 409
    refs = container.dataset_service.list_refs("tenant_test", task_id)
    assert len(refs) == 1
    assert refs[0]["workspace_path"] == created.json()["workspace_path"]

    denied = client.post(
        f"/api/v1/tasks/{task_id}/data-files",
        headers={"Idempotency-Key": "wrong-owner", "X-User-ID": "other"},
        files=files,
    )
    assert denied.status_code == 404

    safe = client.post(
        f"/api/v1/tasks/{task_id}/data-files",
        headers={"Idempotency-Key": "safe-path"},
        files={"file": ("../../danger.csv", b"value\n1\n", "text/csv")},
    )
    assert safe.status_code == 201
    assert "/../" not in safe.json()["workspace_path"]

    unsupported = client.post(
        f"/api/v1/tasks/{task_id}/data-files",
        headers={"Idempotency-Key": "wrong-media"},
        files={"file": ("payload.bin", b"not-tabular", "application/octet-stream")},
    )
    assert unsupported.status_code == 415

    container.settings = replace(
        container.settings,
        phase6_limits=replace(
            container.settings.phase6_limits,
            data_upload_max_bytes=4,
        ),
    )
    oversized = client.post(
        f"/api/v1/tasks/{task_id}/data-files",
        headers={"Idempotency-Key": "oversized"},
        files={"file": ("large.csv", b"12345", "text/csv")},
    )
    assert oversized.status_code == 413
