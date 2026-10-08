from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from threading import Barrier

from fastapi.testclient import TestClient
from sqlalchemy import func, select

from src.api_view.web_main import create_app
from src.api_view.auth import AuthService
from src.agent.tools.database_read import DatabaseCredential
from src.agent.identity import content_hash
from src.agent.schema.state import Evidence, ReportArtifact, ResearchState
from src.agent.backends.persistence.models import (
    ConversationMessageRow,
    DataSourceRow,
    DataSourceTablePolicyRow,
    DataSourceUserGrantRow,
    DocumentRow,
    AuditLogRow,
    ModelBindingRow,
    ModelConfigHeadRow,
    ModelConfigRevocationRow,
    ModelConfigRow,
    OutboxMessageRow,
    ResearchCheckpointRow,
    ResearchTaskRow,
)
from src.agent.errors import InvalidRefreshSessionError


ORIGIN = "https://frontend.test"
PASSWORD = "correct horse battery staple"


def _auth_app(container, session_factory, *, same_site: str = "lax"):
    container.settings = replace(
        container.settings,
        auth_enabled=True,
        auth_cookie_secure=True,
        auth_cookie_samesite=same_site,
        auth_allowed_origins=(ORIGIN,),
    )
    identities = AuthService(session_factory, container.settings)
    identities.create_identity(
        tenant_id="tenant_test",
        tenant_name="Test Tenant",
        user_id="user_test",
        email="admin@example.test",
        password=PASSWORD,
        role="admin",
    )
    identities.create_identity(
        tenant_id="tenant_test",
        tenant_name="Test Tenant",
        user_id="user_other",
        email="member@example.test",
        password=PASSWORD,
        role="member",
    )
    identities.create_identity(
        tenant_id="tenant_other",
        tenant_name="Other Tenant",
        user_id="user_foreign",
        email="foreign@example.test",
        password=PASSWORD,
        role="member",
    )
    app = create_app(container)
    return app, TestClient(app, base_url="https://api.test")


def _login(client: TestClient, email: str, tenant_id: str) -> tuple[str, dict[str, str]]:
    response = client.post(
        "/api/v1/auth/login",
        json={"email": email, "password": PASSWORD, "tenant_id": tenant_id},
    )
    assert response.status_code == 200
    body = response.json()
    assert "refresh_token" not in body
    return body["access_token"], {"Authorization": f"Bearer {body['access_token']}"}


class _AdminCredentialResolver:
    def resolve(self, credential_ref: str, mode: str) -> DatabaseCredential:
        assert credential_ref == "TEST_DATABASE_CREDENTIAL"
        return DatabaseCredential(username=f"{mode}_user", password="not-exposed")


class _AdminDatabase:
    def execute_readonly(self, sql: str, *, row_limit: int, timeout_ms: int):
        assert sql == "SELECT 1"
        assert row_limit == 1
        assert timeout_ms == 5_000
        return ["?column?"], [[1]], False


class _AdminDatabaseFactory:
    def get(self, datasource, credential, access_mode):
        del datasource, credential, access_mode
        return _AdminDatabase()


def test_browser_cors_preflight_allows_frozen_auth_and_sse_headers(
    make_container, session_factory
) -> None:
    container, _ = make_container({})
    _app, client = _auth_app(container, session_factory)
    response = client.options(
        "/api/v1/tasks/task_test/events",
        headers={
            "Origin": ORIGIN,
            "Access-Control-Request-Method": "GET",
            "Access-Control-Request-Headers": "authorization,last-event-id,x-request-id",
        },
    )
    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == ORIGIN
    assert response.headers["access-control-allow-credentials"] == "true"
    allowed = response.headers["access-control-allow-headers"].lower()
    assert "authorization" in allowed
    assert "last-event-id" in allowed
    assert "x-request-id" in allowed


def test_auth_cookie_rotation_logout_and_problem_contract(make_container, session_factory) -> None:
    container, _ = make_container({})
    _app, client = _auth_app(container, session_factory)

    unauthenticated = client.get("/api/v1/tasks")
    assert unauthenticated.status_code == 401
    assert unauthenticated.json()["code"] == "unauthenticated"
    assert unauthenticated.headers["content-type"].startswith("application/problem+json")
    assert unauthenticated.headers["www-authenticate"] == "Bearer"
    assert unauthenticated.headers["x-request-id"]

    bad = client.post(
        "/api/v1/auth/login",
        json={
            "email": "admin@example.test",
            "password": "wrong password",
            "tenant_id": "tenant_test",
        },
    )
    assert bad.status_code == 401

    _token, headers = _login(client, "admin@example.test", "tenant_test")
    set_cookie = client.post(
        "/api/v1/auth/login",
        json={
            "email": "admin@example.test",
            "password": PASSWORD,
            "tenant_id": "tenant_test",
        },
    ).headers.get_list("set-cookie")[0]
    assert "HttpOnly" in set_cookie
    assert "Secure" in set_cookie
    assert "SameSite=lax" in set_cookie
    assert "Path=/api/v1/auth" in set_cookie
    assert client.get("/api/v1/tasks", headers=headers).status_code == 200

    old_refresh = client.cookies.get(container.settings.auth_refresh_cookie_name)
    assert old_refresh
    assert client.post("/api/v1/auth/refresh").status_code == 403
    refreshed = client.post("/api/v1/auth/refresh", headers={"Origin": ORIGIN})
    assert refreshed.status_code == 200
    assert refreshed.json()["access_token"]
    new_refresh = client.cookies.get(container.settings.auth_refresh_cookie_name)
    assert new_refresh and new_refresh != old_refresh

    replay_client = TestClient(_app, base_url="https://api.test")
    replay = replay_client.post(
        "/api/v1/auth/refresh",
        headers={
            "Origin": ORIGIN,
            "Cookie": f"{container.settings.auth_refresh_cookie_name}={old_refresh}",
        },
    )
    assert replay.status_code == 401
    assert replay.json()["code"] == "invalid_refresh_session"

    logged_out = client.post("/api/v1/auth/logout", headers={"Origin": ORIGIN})
    assert logged_out.status_code == 204
    rejected = replay_client.post(
        "/api/v1/auth/refresh",
        headers={
            "Origin": ORIGIN,
            "Cookie": f"{container.settings.auth_refresh_cookie_name}={new_refresh}",
        },
    )
    assert rejected.status_code == 401


def test_same_site_none_requires_double_submit_csrf(make_container, session_factory) -> None:
    container, _ = make_container({})
    _app, client = _auth_app(container, session_factory, same_site="none")
    _login(client, "admin@example.test", "tenant_test")
    csrf = client.cookies.get("dr_csrf")
    assert csrf
    assert client.post("/api/v1/auth/refresh", headers={"Origin": ORIGIN}).status_code == 403
    accepted = client.post(
        "/api/v1/auth/refresh",
        headers={"Origin": ORIGIN, "X-CSRF-Token": csrf},
    )
    assert accepted.status_code == 200
    assert "SameSite=none" in accepted.headers.get_list("set-cookie")[0]


def test_concurrent_refresh_has_one_cas_winner(make_container, session_factory) -> None:
    container, _ = make_container({})
    app, client = _auth_app(container, session_factory)
    _login(client, "admin@example.test", "tenant_test")
    old_refresh = client.cookies.get(container.settings.auth_refresh_cookie_name)
    assert old_refresh
    service = app.state.auth_service
    barrier = Barrier(2)

    def rotate() -> str:
        barrier.wait()
        try:
            return service.refresh(old_refresh).response.access_token
        except InvalidRefreshSessionError:
            return "rejected"

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(lambda _index: rotate(), range(2)))
    assert outcomes.count("rejected") == 1
    assert sum(item != "rejected" for item in outcomes) == 1


def test_task_http_creation_is_atomic_idempotent_and_opaque(
    make_container, session_factory
) -> None:
    container, _ = make_container({})
    _app, client = _auth_app(container, session_factory)
    _token, headers = _login(client, "admin@example.test", "tenant_test")
    headers = {**headers, "Idempotency-Key": "phase10-create"}
    request = {
        "query": "Research the durable HTTP boundary",
        "options": {"max_depth": 9, "max_revision_rounds": 1},
    }
    created = client.post("/api/v1/tasks", headers=headers, json=request)
    assert created.status_code == 201
    body = created.json()
    assert body["task_status"] == "pending"
    assert body["phase"] == "routing"
    assert body["message_id"]
    assert not {
        "latest_state",
        "lease_token",
        "model_binding_snapshot",
        "capability_snapshot",
        "task_budget",
    } & body.keys()

    replay = client.post("/api/v1/tasks", headers=headers, json=request)
    assert replay.status_code == 200
    assert replay.json() == body
    conflict = client.post(
        "/api/v1/tasks",
        headers=headers,
        json={"query": "A different request"},
    )
    assert conflict.status_code == 409
    assert conflict.json()["code"] == "idempotency_conflict"

    with session_factory() as session:
        task = session.get(ResearchTaskRow, body["task_id"])
        assert task is not None
        assert task.status == "pending"
        assert task.latest_state["execution"]["max_depth"] == 5
        assert task.model_binding_snapshot["bindings"]
        assert session.scalar(
            select(func.count()).select_from(ConversationMessageRow).where(
                ConversationMessageRow.task_id == body["task_id"]
            )
        ) == 1
        checkpoint = session.scalar(
            select(ResearchCheckpointRow).where(
                ResearchCheckpointRow.task_id == body["task_id"]
            )
        )
        assert checkpoint is not None and checkpoint.checkpoint_seq == 0
        outbox = session.scalar(
            select(OutboxMessageRow).where(
                OutboxMessageRow.aggregate_id == body["task_id"],
                OutboxMessageRow.kind == "research.schedule",
            )
        )
        assert outbox is not None and outbox.dispatched_at is None


def test_owner_child_resource_and_datasource_authorization(
    make_container, session_factory
) -> None:
    container, _ = make_container({})
    app, owner_client = _auth_app(container, session_factory)
    _owner_token, owner = _login(owner_client, "admin@example.test", "tenant_test")
    created = owner_client.post(
        "/api/v1/tasks",
        headers={**owner, "Idempotency-Key": "owner-task"},
        json={"query": "authorization boundary"},
    ).json()
    task_id = created["task_id"]

    member_client = TestClient(app, base_url="https://api.test")
    _member_token, member = _login(member_client, "member@example.test", "tenant_test")
    foreign_client = TestClient(app, base_url="https://api.test")
    _foreign_token, foreign = _login(
        foreign_client, "foreign@example.test", "tenant_other"
    )
    for headers in (member, foreign):
        assert member_client.get(f"/api/v1/tasks/{task_id}", headers=headers).status_code == 404
        assert member_client.get(
            f"/api/v1/tasks/{task_id}/evidence", headers=headers
        ).status_code == 404
        assert member_client.get(
            f"/api/v1/tasks/{task_id}/workspace", headers=headers
        ).status_code == 404
        assert member_client.get(
            f"/api/v1/tasks/{task_id}/workspace/../../secret", headers=headers
        ).status_code == 404
        assert member_client.post(
            f"/api/v1/tasks/{task_id}/knowledge-graph/query",
            headers=headers,
            json={"op": "search", "query": "secret"},
        ).status_code == 404
        assert member_client.get(
            f"/api/v1/tasks/{task_id}/events", headers=headers
        ).status_code == 404

    with session_factory() as session, session.begin():
        session.add(
            DocumentRow(
                doc_id="doc_shared",
                tenant_id="tenant_test",
                name="shared.txt",
                media_type="text/plain",
                storage_ref="private/minio/key",
                content_hash="a" * 64,
                size_bytes=6,
                status="ready",
            )
        )
        session.add(
            DataSourceRow(
                datasource_id="ds_shared",
                tenant_id="tenant_test",
                name="Warehouse",
                host="localhost",
                port=5432,
                database_name="warehouse",
                ssl_mode="require",
                credential_ref="PRIVATE_DATABASE_SECRET",
                enabled=True,
            )
        )
        session.flush()
        session.add(
            DataSourceUserGrantRow(
                tenant_id="tenant_test",
                datasource_id="ds_shared",
                user_id="user_other",
                access_level="read",
            )
        )
        session.add(
            DataSourceTablePolicyRow(
                tenant_id="tenant_test",
                datasource_id="ds_shared",
                schema_name="public",
                table_name="facts",
                readable_columns=["id"],
                writable_columns=[],
                max_write_rows=0,
            )
        )

    document = member_client.get("/api/v1/documents/doc_shared", headers=member)
    assert document.status_code == 200
    assert "storage_ref" not in document.json()
    assert foreign_client.get(
        "/api/v1/documents/doc_shared", headers=foreign
    ).status_code == 404
    sources = member_client.get("/api/v1/datasources", headers=member).json()["items"]
    assert sources == [
        {
            "datasource_id": "ds_shared",
            "name": "Warehouse",
            "enabled": True,
            "access_level": "read",
            "write_ready": False,
        }
    ]
    assert "credential_ref" not in str(sources)


def test_signed_cursor_rejects_tampering(make_container, session_factory) -> None:
    container, _ = make_container({})
    _app, client = _auth_app(container, session_factory)
    _token, headers = _login(client, "admin@example.test", "tenant_test")
    for index in range(2):
        response = client.post(
            "/api/v1/tasks",
            headers={**headers, "Idempotency-Key": f"cursor-{index}"},
            json={"query": f"task {index}"},
        )
        assert response.status_code == 201
    page = client.get("/api/v1/tasks?limit=1", headers=headers).json()
    cursor = page["page"]["next_cursor"]
    assert cursor
    replacement = "A" if cursor[-1] != "A" else "B"
    rejected = client.get(
        f"/api/v1/tasks?limit=1&cursor={cursor[:-1]}{replacement}", headers=headers
    )
    assert rejected.status_code == 422
    assert rejected.json()["code"] == "invalid_cursor"


def test_task_control_http_contract_and_pending_write_gate(
    make_container, session_factory
) -> None:
    container, _ = make_container({})
    _app, client = _auth_app(container, session_factory)
    _token, headers = _login(client, "admin@example.test", "tenant_test")
    created = client.post(
        "/api/v1/tasks",
        headers={**headers, "Idempotency-Key": "control-create"},
        json={"query": "control contract"},
    ).json()
    task_id = created["task_id"]

    paused = client.post(
        f"/api/v1/tasks/{task_id}/pause",
        headers={**headers, "Idempotency-Key": "control-pause"},
    )
    assert paused.status_code == 202
    assert paused.json()["task_status"] == "paused"
    replay = client.post(
        f"/api/v1/tasks/{task_id}/pause",
        headers={**headers, "Idempotency-Key": "control-pause"},
    )
    assert replay.status_code == 202
    assert replay.json() == paused.json()
    conflict = client.post(
        f"/api/v1/tasks/{task_id}/cancel",
        headers={**headers, "Idempotency-Key": "control-pause"},
    )
    assert conflict.status_code == 409
    assert conflict.json()["code"] == "idempotency_conflict"

    with session_factory() as session, session.begin():
        task = session.get(ResearchTaskRow, task_id)
        assert task is not None
        state = ResearchState.model_validate(task.latest_state)
        state.pending_write = "write_waiting"
        task.latest_state = state.model_dump(mode="json")
    gated = client.post(
        f"/api/v1/tasks/{task_id}/resume",
        headers={**headers, "Idempotency-Key": "control-resume"},
    )
    assert gated.status_code == 409
    assert gated.json()["code"] == "invalid_state"


def test_evidence_workspace_report_citation_and_kg_api_projections(
    make_container, session_factory
) -> None:
    container, _ = make_container({})
    _app, client = _auth_app(container, session_factory)
    _token, headers = _login(client, "admin@example.test", "tenant_test")
    created = client.post(
        "/api/v1/tasks",
        headers={**headers, "Idempotency-Key": "resource-create"},
        json={"query": "resource projections"},
    ).json()
    task_id = created["task_id"]
    data = {"rows": [["grounded"]]}
    evidence = Evidence(
        evidence_id="ev_phase10",
        hypothesis_id="h1",
        summary="Grounded result",
        excerpt="grounded",
        source_type="sql",
        stance="support",
        relevance=1,
        data=data,
        content_hash=content_hash(data),
    )
    report = {
        "report_version": 1,
        "title": "Phase 10 report",
        "sections": [
            {
                "section_id": "summary",
                "heading": "Summary",
                "level": 2,
                "blocks": [
                    {
                        "type": "paragraph",
                        "segments": [
                            {
                                "type": "claim",
                                "claim_id": "claim_1",
                                "text": "The result is grounded.",
                                "citation_ids": ["cite_1"],
                            }
                        ],
                    }
                ],
            }
        ],
        "citations": [{"citation_id": "cite_1", "evidence_id": "ev_phase10"}],
    }
    report_file = container.workspace_service.publish_bytes(
        tenant_id="tenant_test",
        task_id=task_id,
        path="reports/versions/1/report.json",
        data=json.dumps(report).encode(),
        mime_type="application/json",
        operation_key="phase10-report",
    )
    container.workspace_service.publish_bytes(
        tenant_id="tenant_test",
        task_id=task_id,
        path="files/result.txt",
        data=b"0123456789",
        mime_type="text/plain",
        operation_key="phase10-result",
    )
    with session_factory() as session, session.begin():
        task = session.get(ResearchTaskRow, task_id)
        assert task is not None
        state = ResearchState.model_validate(task.latest_state)
        state.evidence_pool = [evidence]
        state.report.final = ReportArtifact(
            version=1,
            workspace_path=report_file.workspace_path,
            content_hash=report_file.content_hash,
        )
        task.latest_state = state.model_dump(mode="json")

    evidence_list = client.get(
        f"/api/v1/tasks/{task_id}/evidence", headers=headers
    )
    assert evidence_list.status_code == 200
    assert evidence_list.json()["items"][0]["evidence_id"] == "ev_phase10"
    assert client.get(
        f"/api/v1/tasks/{task_id}/evidence/ev_phase10", headers=headers
    ).status_code == 200
    workspace = client.get(
        f"/api/v1/tasks/{task_id}/workspace?glob=files/*.txt", headers=headers
    )
    assert workspace.status_code == 200
    assert workspace.json()["files"][0]["workspace_path"] == "files/result.txt"
    assert "storage_ref" not in str(workspace.json())
    ranged = client.get(
        f"/api/v1/tasks/{task_id}/workspace/files/result.txt",
        headers={**headers, "Range": "bytes=2-5"},
    )
    assert ranged.status_code == 206
    assert ranged.content == b"2345"
    report_response = client.get(f"/api/v1/tasks/{task_id}/report", headers=headers)
    assert report_response.status_code == 200
    assert report_response.json()["report"]["title"] == "Phase 10 report"
    citations = client.get(
        f"/api/v1/tasks/{task_id}/report/citations", headers=headers
    )
    assert citations.json()["citations"] == [
        {"citation_id": "cite_1", "evidence_id": "ev_phase10", "locator": None}
    ]
    graph = client.post(
        f"/api/v1/tasks/{task_id}/knowledge-graph/query",
        headers=headers,
        json={"op": "search", "query": "grounded"},
    )
    assert graph.status_code == 200
    assert graph.json()["graph_status"] == "building"


def test_admin_datasource_and_model_config_lifecycle(
    make_container, session_factory
) -> None:
    container, _runtime = make_container({"reasoning": ["OK"]})
    container.datasource_service.credentials = _AdminCredentialResolver()
    container.datasource_service.databases = _AdminDatabaseFactory()
    _app, client = _auth_app(container, session_factory)
    _token, admin = _login(client, "admin@example.test", "tenant_test")
    _token, member = _login(client, "member@example.test", "tenant_test")

    denied = client.post(
        "/api/v1/datasources",
        headers=member,
        json={
            "name": "denied",
            "host": "localhost",
            "database_name": "research",
            "credential_ref": "TEST_DATABASE_CREDENTIAL",
        },
    )
    assert denied.status_code == 403

    created = client.post(
        "/api/v1/datasources",
        headers=admin,
        json={
            "name": "warehouse",
            "host": "localhost",
            "database_name": "research",
            "credential_ref": "TEST_DATABASE_CREDENTIAL",
        },
    )
    assert created.status_code == 201
    datasource_id = created.json()["datasource_id"]
    assert created.json()["enabled"] is False
    assert "credential_ref" not in created.text
    assert client.put(
        f"/api/v1/datasources/{datasource_id}/policies",
        headers=admin,
        json={
            "policies": [
                {
                    "schema_name": "public",
                    "table_name": "facts",
                    "readable_columns": ["id", "value"],
                    "writable_columns": ["value"],
                    "allow_update": True,
                    "max_write_rows": 10,
                }
            ]
        },
    ).status_code == 204
    assert client.put(
        f"/api/v1/datasources/{datasource_id}/grants/user_other",
        headers=admin,
        json={"access_level": "write"},
    ).status_code == 204
    assert client.post(
        f"/api/v1/datasources/{datasource_id}/test", headers=admin
    ).status_code == 200
    assert client.post(
        f"/api/v1/datasources/{datasource_id}/enable", headers=admin
    ).status_code == 200
    member_items = client.get("/api/v1/datasources", headers=member).json()["items"]
    assert member_items == [
        {
            "datasource_id": datasource_id,
            "name": "warehouse",
            "enabled": True,
            "access_level": "write",
            "write_ready": True,
        }
    ]
    patched = client.patch(
        f"/api/v1/datasources/{datasource_id}",
        headers=admin,
        json={"name": "warehouse-v2"},
    )
    assert patched.status_code == 200
    assert patched.json()["name"] == "warehouse-v2"
    assert client.post(
        f"/api/v1/datasources/{datasource_id}/disable", headers=admin
    ).json()["enabled"] is False
    assert client.delete(
        f"/api/v1/datasources/{datasource_id}/grants/user_other", headers=admin
    ).status_code == 204

    model = client.post(
        "/api/v1/model-configs",
        headers=admin,
        json={
            "model_config_id": "model_admin_api",
            "provider": "fake",
            "model": "test-v1",
            "purpose": "chat",
            "secret_ref": "MODEL_SECRET_REF",
        },
    )
    assert model.status_code == 201
    assert model.json()["version"] == 1
    assert model.json()["secret_configured"] is True
    assert "MODEL_SECRET_REF" not in model.text
    assert client.post(
        "/api/v1/model-configs/model_admin_api/test", headers=admin
    ).status_code == 200
    updated = client.patch(
        "/api/v1/model-configs/model_admin_api",
        headers=admin,
        json={"model": "test-v2"},
    )
    assert updated.status_code == 200
    assert updated.json()["version"] == 2
    version_one = client.get(
        "/api/v1/model-configs/model_admin_api/versions/1", headers=admin
    )
    assert version_one.status_code == 200
    assert version_one.json()["model"] == "test-v1"
    assert client.put(
        "/api/v1/model-bindings/reasoning",
        headers=admin,
        json={"model_config_ids": ["model_admin_api"]},
    ).status_code == 204
    assert client.post(
        "/api/v1/model-configs/model_admin_api/disable", headers=admin
    ).json()["enabled_for_new_tasks"] is False
    revoked = client.post(
        "/api/v1/model-configs/model_admin_api/versions/1/revoke",
        headers=admin,
        json={"reason": "security event"},
    )
    assert revoked.status_code == 200

    with session_factory() as session:
        assert session.scalar(select(func.count()).select_from(AuditLogRow)) >= 9
        head = session.get(
            ModelConfigHeadRow, ("tenant_test", "model_admin_api")
        )
        assert head is not None and head.current_version == 2
        assert session.get(ModelConfigRow, ("model_admin_api", 1)) is not None
        assert session.get(ModelConfigRow, ("model_admin_api", 2)) is not None
        assert session.get(
            ModelConfigRevocationRow, ("model_admin_api", 1)
        ) is not None
        binding = session.scalar(
            select(ModelBindingRow).where(
                ModelBindingRow.tenant_id == "tenant_test",
                ModelBindingRow.binding_key == "reasoning",
            )
        )
        assert binding is not None and binding.model_config_id == "model_admin_api"
