from __future__ import annotations

import hashlib
import codecs
import fnmatch
import json
import logging
import re
import uuid
import zipfile
import secrets
import time
from collections.abc import AsyncIterator
from contextvars import ContextVar
from datetime import timedelta
from typing import Annotated, Any, BinaryIO, Literal, cast

from fastapi import FastAPI, File, Header, Query, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response, StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select, text
from starlette.exceptions import HTTPException as StarletteHTTPException

from src.api_view.cursor import CursorCodec
from src.agent.tools.database_read import resolve_database_credential
from src.api_view.auth import (
    AuthContext,
    AuthService,
    LoginRequest,
    RevokeRequest,
    validate_cookie_origin,
    validate_csrf,
)
from src.agent.container import Container, build_container
from src.agent.backends.audit import current_trace_id
from src.agent.schema.state import ResearchState
from src.agent.schema.report import ReportAst
from src.agent.schema.write import ApproveWriteRequest, RejectWriteRequest
from src.agent.memory.domain import ManualEditRequest, RememberRequest, ResolveMemoryRequest
from src.agent.tools.knowledge_graph import KnowledgeGraphQuery
from src.agent.reporting.share_export import (
    CreateExportRequest,
    CreateShareRequest,
    PublicRateLimiter,
)
from src.agent.errors import DomainError
from src.agent.backends.persistence.models import (
    DocumentIngestionRow,
    DocumentRow,
    DocumentUploadReservationRow,
    ConversationMessageRow,
    DataSourceRow,
    DataSourceTablePolicyRow,
    DataSourceUserGrantRow,
    ModelConfigHeadRow,
    ModelConfigRevocationRow,
    ModelConfigRow,
    OutboxMessageRow,
    ResearchTaskRow,
    ThreadRow,
    WorkspaceFileRow,
    utcnow,
)
from src.agent.errors import (
    IdempotencyConflictError,
    IdempotencyInProgressError,
    InvalidArgumentError,
    InvalidCursorError,
    InvalidStateError,
    NotFoundError,
    RangeNotSatisfiableError,
    UnauthenticatedError,
    UnsupportedMediaError,
    UploadTooLargeError,
)
from src.api_view.admin_resources import (
    DataSourceAdminService,
    DataSourceCreate,
    DataSourcePatch,
    ModelConfigAdminService,
    ModelConfigCreate,
    ModelConfigPatch,
    ModelRuntimeForAdmin,
    TablePolicyInput,
    test_model_revision,
)
from src.agent.task_service import CreateTaskRequest, ReplanRequest, new_id


logger = logging.getLogger("uvicorn.error")
SCHEMA_HEAD = "0010_phase14_secret_store"


class CreateThreadRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CreateDataSourceRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=255)
    host: str = Field(min_length=1, max_length=512)
    port: int = Field(default=5432, ge=1, le=65535)
    database_name: str = Field(min_length=1, max_length=255)
    ssl_mode: str = Field(default="require", min_length=1, max_length=32)
    credential_ref: str = Field(min_length=1, max_length=255)


class PatchDataSourceRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str | None = Field(default=None, min_length=1, max_length=255)
    host: str | None = Field(default=None, min_length=1, max_length=512)
    port: int | None = Field(default=None, ge=1, le=65535)
    database_name: str | None = Field(default=None, min_length=1, max_length=255)
    ssl_mode: str | None = Field(default=None, min_length=1, max_length=32)
    credential_ref: str | None = Field(default=None, min_length=1, max_length=255)


class DataSourcePolicyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_name: str = Field(min_length=1, max_length=128)
    table_name: str = Field(min_length=1, max_length=128)
    readable_columns: list[str] = Field(default_factory=list)
    writable_columns: list[str] = Field(default_factory=list)
    allow_insert: bool = False
    allow_update: bool = False
    allow_delete: bool = False
    max_write_rows: int = Field(default=0, ge=0)


class ReplaceDataSourcePoliciesRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    policies: list[DataSourcePolicyRequest]


class PutDataSourceGrantRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    access_level: Literal["read", "write"]


class CreateModelConfigRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    model_config_id: str | None = Field(default=None, min_length=1, max_length=64)
    provider: str = Field(min_length=1, max_length=64)
    model: str = Field(min_length=1, max_length=255)
    base_url: str | None = Field(default=None, max_length=1024)
    purpose: Literal["chat", "embedding", "rerank"]
    secret_ref: str | None = Field(default=None, max_length=255)


class PatchModelConfigRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    provider: str | None = Field(default=None, min_length=1, max_length=64)
    model: str | None = Field(default=None, min_length=1, max_length=255)
    base_url: str | None = Field(default=None, max_length=1024)
    purpose: Literal["chat", "embedding", "rerank"] | None = None
    secret_ref: str | None = Field(default=None, max_length=255)


class RevokeModelConfigRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: str = Field(min_length=1, max_length=1024)


class ReplaceModelBindingRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    model_config_ids: list[str] = Field(min_length=1)


def problem(
    request: Request,
    *,
    status: int,
    code: str,
    title: str,
    detail: str,
    retryable: bool = False,
    errors: list[dict[str, str]] | None = None,
) -> JSONResponse:
    payload: dict[str, object] = {
        "type": f"urn:deepresearch:problem:{code.replace('_', '-')}",
        "title": title,
        "status": status,
        "code": code,
        "detail": detail,
        "instance": _redacted_instance(request.url.path),
        "request_id": getattr(
            request.state,
            "request_id",
            request.headers.get("X-Request-ID", f"req_{uuid.uuid4().hex}"),
        ),
        "retryable": retryable,
    }
    if errors:
        payload["errors"] = errors
    headers: dict[str, str] = {}
    if status == 401:
        headers["WWW-Authenticate"] = "Bearer"
    if status == 429:
        headers["Retry-After"] = "1"
    return JSONResponse(
        payload,
        status_code=status,
        media_type="application/problem+json",
        headers=headers or None,
    )


def _redacted_instance(path: str) -> str:
    return re.sub(
        r"^(/api/v1/public/shares/)[^/]+",
        r"\1[redacted]",
        path,
    )


def create_app(container: Container | None = None) -> FastAPI:
    container = container or build_container()
    app = FastAPI(title="DeepResearch Multi-Agent", version="0.1.0")
    app.state.container = container
    auth_service = AuthService(container.session_factory, container.settings)
    app.state.auth_service = auth_service
    cursor_codec = CursorCodec(
        container.settings.cursor_signing_key,
        container.settings.cursor_ttl_seconds,
    )
    datasource_admin = DataSourceAdminService(
        container.session_factory, container.datasource_service
    )
    model_config_admin = ModelConfigAdminService(container.session_factory)
    cookie_samesite = cast(
        Literal["lax", "strict", "none"], container.settings.auth_cookie_samesite
    )
    current_auth: ContextVar[AuthContext | None] = ContextVar(
        "phase10_auth_context", default=None
    )
    public_share_limiter = PublicRateLimiter(
        requests=container.settings.public_share_rate_limit_requests,
        window_seconds=container.settings.public_share_rate_limit_window_seconds,
    )

    @app.middleware("http")
    async def request_context(request: Request, call_next: Any) -> Response:
        started = time.perf_counter()
        supplied = request.headers.get("X-Request-ID", "")
        request.state.request_id = (
            supplied
            if supplied and len(supplied) <= 128 and re.fullmatch(r"[A-Za-z0-9._:-]+", supplied)
            else f"req_{uuid.uuid4().hex}"
        )
        public_auth_paths = {
            "/api/v1/auth/login",
            "/api/v1/auth/refresh",
            "/api/v1/auth/logout",
        }
        context: AuthContext | None = None
        is_public_share = request.url.path.startswith("/api/v1/public/shares/")
        if (
            request.url.path.startswith("/api/v1/")
            and request.url.path not in public_auth_paths
            and not is_public_share
        ):
            try:
                if container.settings.auth_enabled:
                    context = auth_service.authenticate_access(
                        request.headers.get("Authorization")
                    )
                else:
                    context = AuthContext(
                        tenant_id=request.headers.get(
                            "X-Tenant-ID", container.settings.default_tenant_id
                        ),
                        user_id=request.headers.get(
                            "X-User-ID", container.settings.default_user_id
                        ),
                        roles=("admin",),
                    )
            except DomainError as exc:
                response = problem(
                    request,
                    status=exc.status_code,
                    code=exc.code,
                    title=exc.title,
                    detail=exc.detail,
                    retryable=exc.retryable,
                    errors=exc.errors,
                )
                response.headers["X-Request-ID"] = request.state.request_id
                return response
        request.state.auth_context = context
        token = current_auth.set(context)
        try:
            response = await call_next(request)
            if is_public_share:
                response.headers["Content-Security-Policy"] = (
                    "default-src 'none'; img-src 'self'; style-src 'none'; "
                    "frame-ancestors 'none'; base-uri 'none'; form-action 'none'"
                )
                response.headers["X-Content-Type-Options"] = "nosniff"
                response.headers["Referrer-Policy"] = "no-referrer"
                response.headers["Cache-Control"] = "no-store"
            response.headers["X-Request-ID"] = request.state.request_id
            logger.info(
                "http_request",
                extra={
                    "request_id": request.state.request_id,
                    "tenant_id": context.tenant_id if context else None,
                    "user_id": context.user_id if context else None,
                    "task_id": request.path_params.get("task_id"),
                    "thread_id": request.path_params.get("thread_id"),
                    "http_method": request.method,
                    "http_route": getattr(
                        request.scope.get("route"), "path", request.url.path
                    ),
                    "http_status": response.status_code,
                    "duration_ms": round((time.perf_counter() - started) * 1000, 3),
                },
            )
            return response
        finally:
            current_auth.reset(token)

    @app.exception_handler(DomainError)
    async def domain_error(request: Request, exc: DomainError) -> JSONResponse:
        return problem(
            request,
            status=exc.status_code,
            code=exc.code,
            title=exc.title,
            detail=exc.detail,
            retryable=exc.retryable,
            errors=exc.errors,
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        errors = [
            {
                "field": ".".join(str(item) for item in error["loc"] if item != "body"),
                "code": str(error["type"]),
                "message": str(error["msg"]),
            }
            for error in exc.errors()
        ]
        return problem(
            request,
            status=422,
            code="invalid_argument",
            title="Invalid argument",
            detail="One or more request fields are invalid.",
            errors=errors,
        )

    @app.exception_handler(StarletteHTTPException)
    async def http_error(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        if exc.status_code == 404:
            return problem(
                request,
                status=404,
                code="not_found",
                title="Not found",
                detail="The requested resource was not found.",
            )
        return problem(
            request,
            status=exc.status_code,
            code="invalid_argument",
            title="Invalid request",
            detail="The request cannot be handled by this endpoint.",
        )

    @app.exception_handler(Exception)
    async def internal_error(request: Request, _exc: Exception) -> JSONResponse:
        return problem(
            request,
            status=500,
            code="internal_error",
            title="Internal error",
            detail="The server could not complete the request.",
            retryable=True,
        )

    def identity(
        _x_tenant_id: str | None = None,
        _x_user_id: str | None = None,
    ) -> tuple[str, str]:
        context = current_auth.get()
        if context is None:
            raise UnauthenticatedError("A Bearer access credential is required.")
        return context.tenant_id, context.user_id

    def authenticated_context() -> AuthContext:
        context = current_auth.get()
        if context is None:
            raise UnauthenticatedError("A Bearer access credential is required.")
        return context

    def require_admin() -> None:
        auth_service.require_admin(authenticated_context())

    def set_refresh_cookie(response: Response, credential: str) -> None:
        response.set_cookie(
            container.settings.auth_refresh_cookie_name,
            credential,
            max_age=container.settings.auth_refresh_ttl_days * 86_400,
            httponly=True,
            secure=container.settings.auth_cookie_secure,
            samesite=cookie_samesite,
            path=container.settings.auth_refresh_cookie_path,
        )
        if container.settings.auth_cookie_samesite == "none":
            response.set_cookie(
                "dr_csrf",
                secrets.token_urlsafe(32),
                max_age=container.settings.auth_refresh_ttl_days * 86_400,
                httponly=False,
                secure=True,
                samesite="none",
                path=container.settings.auth_refresh_cookie_path,
            )

    @app.post("/api/v1/auth/login")
    def login(body: LoginRequest) -> JSONResponse:
        result = auth_service.login(body)
        response = JSONResponse(result.response.model_dump(mode="json"))
        set_refresh_cookie(response, result.refresh_credential)
        return response

    @app.post("/api/v1/auth/refresh")
    def refresh_auth(request: Request) -> JSONResponse:
        validate_cookie_origin(request.headers.get("Origin"), container.settings)
        validate_csrf(
            header_token=request.headers.get("X-CSRF-Token"),
            cookie_token=request.cookies.get("dr_csrf"),
            settings=container.settings,
        )
        result = auth_service.refresh(
            request.cookies.get(container.settings.auth_refresh_cookie_name)
        )
        response = JSONResponse(result.response.model_dump(mode="json"))
        set_refresh_cookie(response, result.refresh_credential)
        return response

    @app.post("/api/v1/auth/logout", status_code=204)
    def logout(request: Request) -> Response:
        validate_cookie_origin(request.headers.get("Origin"), container.settings)
        validate_csrf(
            header_token=request.headers.get("X-CSRF-Token"),
            cookie_token=request.cookies.get("dr_csrf"),
            settings=container.settings,
        )
        auth_service.logout(
            request.cookies.get(container.settings.auth_refresh_cookie_name)
        )
        response = Response(status_code=204)
        response.delete_cookie(
            container.settings.auth_refresh_cookie_name,
            path=container.settings.auth_refresh_cookie_path,
            secure=container.settings.auth_cookie_secure,
            httponly=True,
            samesite=cookie_samesite,
        )
        response.delete_cookie(
            "dr_csrf",
            path=container.settings.auth_refresh_cookie_path,
            secure=True,
            samesite="none",
        )
        return response

    @app.post("/api/v1/auth/revoke", status_code=204)
    def revoke_auth(
        body: RevokeRequest,
        request: Request,
    ) -> Response:
        auth = request.state.auth_context
        if not isinstance(auth, AuthContext):
            raise UnauthenticatedError("A Bearer access credential is required.")
        auth_service.revoke(auth, body)
        return Response(status_code=204)

    @app.post("/api/v1/threads", status_code=201)
    def create_thread(_body: CreateThreadRequest | None = None) -> dict[str, object]:
        tenant_id, user_id = identity()
        now = utcnow()
        row = ThreadRow(
            thread_id=new_id("thread"),
            tenant_id=tenant_id,
            user_id=user_id,
            created_at=now,
            updated_at=now,
        )
        with container.session_factory() as session, session.begin():
            session.add(row)
        return {
            "thread_id": row.thread_id,
            "created_at": row.created_at.isoformat(),
            "updated_at": row.updated_at.isoformat(),
        }

    @app.get("/api/v1/threads")
    def list_threads(
        limit: Annotated[int, Query(ge=1, le=100)] = 20,
        cursor: str | None = None,
    ) -> dict[str, object]:
        tenant_id, user_id = identity()
        scope = {"tenant_id": tenant_id, "user_id": user_id}
        last_key = (
            cursor_codec.decode(
                cursor,
                resource="threads",
                scope=scope,
                filters={},
                sort="updated_at_desc",
            )
            if cursor
            else None
        )
        with container.session_factory() as session:
            rows = list(
                session.scalars(
                    select(ThreadRow)
                    .where(
                        ThreadRow.tenant_id == tenant_id,
                        ThreadRow.user_id == user_id,
                    )
                    .order_by(ThreadRow.updated_at.desc(), ThreadRow.thread_id)
                ).all()
            )
        start = _row_start(rows, last_key, "thread_id")
        selected = rows[start : start + limit]
        next_cursor = None
        if selected and start + len(selected) < len(rows):
            last = selected[-1]
            next_cursor = cursor_codec.encode(
                resource="threads",
                scope=scope,
                filters={},
                sort="updated_at_desc",
                last_key=[last.updated_at.isoformat(), last.thread_id],
            )
        return {
            "items": [_thread_projection(row) for row in selected],
            "page": {
                "limit": limit,
                "has_more": next_cursor is not None,
                "next_cursor": next_cursor,
            },
        }

    @app.get("/api/v1/threads/{thread_id}")
    def get_thread(thread_id: str) -> dict[str, object]:
        tenant_id, user_id = identity()
        with container.session_factory() as session:
            row = session.scalar(
                select(ThreadRow).where(
                    ThreadRow.tenant_id == tenant_id,
                    ThreadRow.user_id == user_id,
                    ThreadRow.thread_id == thread_id,
                )
            )
            if row is None:
                raise NotFoundError("Thread was not found.")
            message_count = session.scalar(
                select(func.count())
                .select_from(ConversationMessageRow)
                .where(
                    ConversationMessageRow.tenant_id == tenant_id,
                    ConversationMessageRow.user_id == user_id,
                    ConversationMessageRow.thread_id == thread_id,
                )
            )
            projection = _thread_projection(row)
            projection["message_count"] = int(message_count or 0)
            return projection

    @app.get("/api/v1/threads/{thread_id}/messages")
    def list_thread_messages(
        thread_id: str,
        limit: Annotated[int, Query(ge=1, le=100)] = 20,
        cursor: str | None = None,
    ) -> dict[str, object]:
        tenant_id, user_id = identity()
        get_thread(thread_id)
        scope = {
            "tenant_id": tenant_id,
            "user_id": user_id,
            "thread_id": thread_id,
        }
        last_key = (
            cursor_codec.decode(
                cursor,
                resource="thread_messages",
                scope=scope,
                filters={},
                sort="created_at_asc",
            )
            if cursor
            else None
        )
        with container.session_factory() as session:
            rows = list(
                session.scalars(
                    select(ConversationMessageRow)
                    .where(
                        ConversationMessageRow.tenant_id == tenant_id,
                        ConversationMessageRow.user_id == user_id,
                        ConversationMessageRow.thread_id == thread_id,
                    )
                    .order_by(
                        ConversationMessageRow.created_at,
                        ConversationMessageRow.message_id,
                    )
                ).all()
            )
        start = _row_start(rows, last_key, "message_id")
        selected = rows[start : start + limit]
        next_cursor = None
        if selected and start + len(selected) < len(rows):
            last = selected[-1]
            next_cursor = cursor_codec.encode(
                resource="thread_messages",
                scope=scope,
                filters={},
                sort="created_at_asc",
                last_key=[last.created_at.isoformat(), last.message_id],
            )
        return {
            "items": [
                {
                    "message_id": row.message_id,
                    "task_id": row.task_id,
                    "role": row.role,
                    "content": row.content,
                    "created_at": row.created_at.isoformat(),
                }
                for row in selected
            ],
            "page": {
                "limit": limit,
                "has_more": next_cursor is not None,
                "next_cursor": next_cursor,
            },
        }

    @app.post("/api/v1/tasks", status_code=201)
    def create_task(
        body: CreateTaskRequest,
        request: Request,
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
        x_tenant_id: Annotated[str | None, Header()] = None,
        x_user_id: Annotated[str | None, Header()] = None,
    ) -> JSONResponse:
        tenant_id, user_id = identity(x_tenant_id, x_user_id)
        created = container.task_service.create(tenant_id, user_id, idempotency_key, body)
        return JSONResponse(
            created.projection.model_dump(mode="json", exclude_none=True),
            status_code=200 if created.replayed else 201,
            headers={"Location": f"/api/v1/tasks/{created.projection.task_id}"},
        )

    @app.get("/api/v1/tasks/{task_id}")
    def get_task(
        task_id: str,
        x_tenant_id: Annotated[str | None, Header()] = None,
        x_user_id: Annotated[str | None, Header()] = None,
    ) -> dict[str, object]:
        tenant_id, user_id = identity(x_tenant_id, x_user_id)
        return container.task_service.get(tenant_id, user_id, task_id).model_dump(
            mode="json", exclude_none=True
        )

    @app.get("/api/v1/tasks")
    def list_tasks(
        thread_id: str | None = None,
        task_status: str | None = None,
        limit: Annotated[int, Query(ge=1, le=100)] = 20,
        cursor: str | None = None,
    ) -> dict[str, object]:
        tenant_id, user_id = identity()
        allowed_statuses = {
            "pending",
            "running",
            "pausing",
            "paused",
            "cancelling",
            "cancelled",
            "completed",
            "failed",
        }
        if task_status is not None and task_status not in allowed_statuses:
            raise InvalidArgumentError("The task_status filter is invalid.")
        filters = {"thread_id": thread_id, "task_status": task_status}
        scope = {"tenant_id": tenant_id, "user_id": user_id}
        last_key = (
            cursor_codec.decode(
                cursor,
                resource="tasks",
                scope=scope,
                filters=filters,
                sort="updated_at_desc",
            )
            if cursor
            else None
        )
        with container.session_factory() as session:
            statement = select(ResearchTaskRow).where(
                ResearchTaskRow.tenant_id == tenant_id,
                ResearchTaskRow.user_id == user_id,
                ResearchTaskRow.deleted_at.is_(None),
            )
            if thread_id:
                statement = statement.where(ResearchTaskRow.thread_id == thread_id)
            if task_status:
                statement = statement.where(ResearchTaskRow.status == task_status)
            rows = list(
                session.scalars(
                    statement.order_by(
                        ResearchTaskRow.updated_at.desc(), ResearchTaskRow.task_id
                    )
                ).all()
            )
        start = _row_start(rows, last_key, "task_id")
        selected = rows[start : start + limit]
        next_cursor = None
        if selected and start + len(selected) < len(rows):
            last = selected[-1]
            next_cursor = cursor_codec.encode(
                resource="tasks",
                scope=scope,
                filters=filters,
                sort="updated_at_desc",
                last_key=[last.updated_at.isoformat(), last.task_id],
            )
        return {
            "items": [
                container.task_service.get(tenant_id, user_id, row.task_id).model_dump(
                    mode="json", exclude_none=True
                )
                for row in selected
            ],
            "page": {
                "limit": limit,
                "has_more": next_cursor is not None,
                "next_cursor": next_cursor,
            },
        }

    @app.delete("/api/v1/tasks/{task_id}", status_code=202)
    def delete_task(task_id: str) -> dict[str, object]:
        tenant_id, user_id = identity()
        container.task_service.delete(tenant_id, user_id, task_id)
        return {"task_id": task_id, "deleted": True}

    @app.get("/api/v1/tasks/{task_id}/evidence")
    def list_evidence(
        task_id: str,
        source_type: Literal["web", "rag", "sql", "compute"] | None = None,
        limit: Annotated[int, Query(ge=1, le=100)] = 20,
        cursor: str | None = None,
    ) -> dict[str, object]:
        tenant_id, user_id = identity()
        container.task_service.get(tenant_id, user_id, task_id)
        state = _owned_task_state(container, tenant_id, user_id, task_id)
        items = [
            item
            for item in state.evidence_pool
            if source_type is None or item.source_type == source_type
        ]
        scope = {"tenant_id": tenant_id, "user_id": user_id, "task_id": task_id}
        filters = {"source_type": source_type}
        last_id = (
            cursor_codec.decode(
                cursor,
                resource="task_evidence",
                scope=scope,
                filters=filters,
                sort="state_order",
            )
            if cursor
            else None
        )
        start = 0
        if last_id is not None:
            try:
                start = next(
                    index + 1
                    for index, item in enumerate(items)
                    if item.evidence_id == last_id
                )
            except StopIteration as exc:
                raise InvalidCursorError("Evidence cursor is no longer valid.") from exc
        selected = items[start : start + limit]
        next_cursor = None
        if selected and start + len(selected) < len(items):
            next_cursor = cursor_codec.encode(
                resource="task_evidence",
                scope=scope,
                filters=filters,
                sort="state_order",
                last_key=selected[-1].evidence_id,
            )
        return {
            "items": [
                item.model_dump(mode="json", exclude_none=True) for item in selected
            ],
            "page": {
                "limit": limit,
                "has_more": next_cursor is not None,
                "next_cursor": next_cursor,
            },
        }

    @app.get("/api/v1/tasks/{task_id}/evidence/{evidence_id}")
    def get_evidence(task_id: str, evidence_id: str) -> dict[str, object]:
        tenant_id, user_id = identity()
        state = _owned_task_state(container, tenant_id, user_id, task_id)
        for item in state.evidence_pool:
            if item.evidence_id == evidence_id:
                return item.model_dump(mode="json", exclude_none=True)
        raise NotFoundError("Evidence was not found.")

    @app.get("/api/v1/tasks/{task_id}/report")
    def get_report(task_id: str) -> dict[str, object]:
        tenant_id, user_id = identity()
        state = _owned_task_state(container, tenant_id, user_id, task_id)
        reference = state.report.final
        if reference is None:
            raise NotFoundError("A final report is not available for this task.")
        raw = container.workspace_service.read(
            tenant_id, task_id, reference.workspace_path
        )
        try:
            report = ReportAst.model_validate(json.loads(raw))
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            raise InvalidStateError("The published report is not readable.") from exc
        return {
            "task_id": task_id,
            "version": reference.version,
            "workspace_path": reference.workspace_path,
            "content_hash": reference.content_hash,
            "report": report.model_dump(mode="json"),
        }

    @app.get("/api/v1/tasks/{task_id}/report/citations")
    def get_report_citations(task_id: str) -> dict[str, object]:
        report = get_report(task_id)
        ast = ReportAst.model_validate(report["report"])
        return {
            "task_id": task_id,
            "report_version": ast.report_version,
            "citations": [item.model_dump(mode="json") for item in ast.citations],
        }

    @app.post("/api/v1/shares", status_code=201)
    def create_share(body: CreateShareRequest) -> dict[str, object]:
        tenant_id, user_id = identity()
        return container.share_manager.create(tenant_id, user_id, body)

    @app.get("/api/v1/shares")
    def list_shares() -> dict[str, object]:
        tenant_id, user_id = identity()
        return {"items": container.share_manager.list_owned(tenant_id, user_id)}

    @app.delete("/api/v1/shares/{share_id}", status_code=204)
    def revoke_share(share_id: str) -> Response:
        tenant_id, user_id = identity()
        container.share_manager.revoke(tenant_id, user_id, share_id)
        return Response(status_code=204)

    @app.get("/api/v1/public/shares/{token}")
    def read_public_share(token: str, request: Request) -> JSONResponse:
        client = request.client.host if request.client else "unknown"
        public_share_limiter.acquire(f"snapshot:{client}")
        _row, snapshot = container.share_manager.read_public(token)
        return JSONResponse(snapshot)

    @app.get("/api/v1/public/shares/{token}/assets/{asset_id}")
    def read_public_share_asset(token: str, asset_id: str, request: Request) -> Response:
        client = request.client.host if request.client else "unknown"
        public_share_limiter.acquire(f"asset:{client}")
        _row, payload, media_type, digest = container.share_manager.read_asset(
            token, asset_id
        )
        return Response(
            payload,
            media_type=media_type,
            headers={"ETag": f'"{digest}"'},
        )

    @app.post("/api/v1/tasks/{task_id}/report/exports", status_code=202)
    def create_report_export(
        task_id: str,
        body: CreateExportRequest,
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
    ) -> JSONResponse:
        tenant_id, user_id = identity()
        result, replayed = container.export_manager.create(
            tenant_id, user_id, task_id, idempotency_key, body
        )
        return JSONResponse(result, status_code=200 if replayed else 202)

    @app.get("/api/v1/tasks/{task_id}/report/exports/{export_id}")
    def get_report_export(task_id: str, export_id: str) -> dict[str, object]:
        tenant_id, user_id = identity()
        return container.export_manager.get(tenant_id, user_id, task_id, export_id)

    @app.get("/api/v1/tasks/{task_id}/report/exports/{export_id}/content")
    def download_report_export(task_id: str, export_id: str) -> Response:
        tenant_id, user_id = identity()
        payload, media_type, digest, format = container.export_manager.content(
            tenant_id, user_id, task_id, export_id
        )
        filename = _safe_header_filename(f"report-{export_id}.{format}")
        return Response(
            payload,
            media_type=media_type,
            headers={
                "Content-Disposition": f'attachment; filename="{filename}"',
                "ETag": f'"{digest}"',
            },
        )

    @app.post("/api/v1/tasks/{task_id}/replan", status_code=202)
    def replan_task(
        task_id: str,
        body: ReplanRequest,
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
        x_tenant_id: Annotated[str | None, Header()] = None,
        x_user_id: Annotated[str | None, Header()] = None,
    ) -> dict[str, object]:
        tenant_id, user_id = identity(x_tenant_id, x_user_id)
        return container.task_service.replan(
            tenant_id, user_id, task_id, idempotency_key, body
        ).model_dump(mode="json", exclude_none=True)

    @app.post("/api/v1/tasks/{task_id}/pause", status_code=202)
    def pause_task(
        task_id: str,
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
        x_tenant_id: Annotated[str | None, Header()] = None,
        x_user_id: Annotated[str | None, Header()] = None,
    ) -> dict[str, object]:
        tenant_id, user_id = identity(x_tenant_id, x_user_id)
        return container.task_service.command(
            tenant_id, user_id, task_id, "pause", idempotency_key
        ).model_dump(mode="json", exclude_none=True)

    @app.post("/api/v1/tasks/{task_id}/resume", status_code=202)
    def resume_task(
        task_id: str,
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
        x_tenant_id: Annotated[str | None, Header()] = None,
        x_user_id: Annotated[str | None, Header()] = None,
    ) -> dict[str, object]:
        tenant_id, user_id = identity(x_tenant_id, x_user_id)
        return container.task_service.command(
            tenant_id, user_id, task_id, "resume", idempotency_key
        ).model_dump(mode="json", exclude_none=True)

    @app.post("/api/v1/tasks/{task_id}/cancel", status_code=202)
    def cancel_task(
        task_id: str,
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
        x_tenant_id: Annotated[str | None, Header()] = None,
        x_user_id: Annotated[str | None, Header()] = None,
    ) -> dict[str, object]:
        tenant_id, user_id = identity(x_tenant_id, x_user_id)
        return container.task_service.command(
            tenant_id, user_id, task_id, "cancel", idempotency_key
        ).model_dump(mode="json", exclude_none=True)

    @app.post("/api/v1/tasks/{task_id}/knowledge-graph/query")
    def query_task_knowledge_graph(
        task_id: str,
        body: KnowledgeGraphQuery,
        x_tenant_id: Annotated[str | None, Header()] = None,
        x_user_id: Annotated[str | None, Header()] = None,
    ) -> dict[str, object]:
        tenant_id, user_id = identity(x_tenant_id, x_user_id)
        return container.knowledge_graph_service.query(
            tenant_id, user_id, task_id, body
        ).model_dump(mode="json", exclude_none=True)

    @app.get("/api/v1/memories")
    def list_memories(
        status: str | None = None,
        category: str | None = None,
        topic: str | None = None,
        limit: Annotated[int, Query(ge=1, le=100)] = 20,
        x_tenant_id: Annotated[str | None, Header()] = None,
        x_user_id: Annotated[str | None, Header()] = None,
    ) -> dict[str, object]:
        tenant_id, user_id = identity(x_tenant_id, x_user_id)
        items = container.memory_service.list(
            tenant_id=tenant_id,
            user_id=user_id,
            status=status,
            category=category,
            topic=topic,
            limit=limit,
        )
        return {
            "items": [item.model_dump(mode="json") for item in items],
            "page": {"limit": limit, "has_more": False},
        }

    @app.post("/api/v1/memories", status_code=201)
    def remember_memory(
        body: RememberRequest,
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
        x_tenant_id: Annotated[str | None, Header()] = None,
        x_user_id: Annotated[str | None, Header()] = None,
    ) -> dict[str, object]:
        tenant_id, user_id = identity(x_tenant_id, x_user_id)
        result = container.memory_service.remember(
            tenant_id=tenant_id,
            user_id=user_id,
            topic=body.topic,
            content=body.content,
            supporting_message_ids=body.supporting_message_ids,
            logical_key=f"explicit:{idempotency_key}",
        )
        return result.model_dump(mode="json")

    @app.patch("/api/v1/memories/{memory_id}")
    def edit_memory(
        memory_id: str,
        body: ManualEditRequest,
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
        x_tenant_id: Annotated[str | None, Header()] = None,
        x_user_id: Annotated[str | None, Header()] = None,
    ) -> dict[str, object]:
        tenant_id, user_id = identity(x_tenant_id, x_user_id)
        result = container.memory_service.manual_edit(
            tenant_id=tenant_id,
            user_id=user_id,
            memory_id=memory_id,
            expected_revision=body.expected_revision,
            topic=body.topic,
            content=body.content,
            supporting_message_ids=body.supporting_message_ids or None,
            logical_key=f"manual:{idempotency_key}",
        )
        return result.model_dump(mode="json")

    @app.post("/api/v1/memories/{memory_id}/resolve")
    def resolve_memory(
        memory_id: str,
        body: ResolveMemoryRequest,
        x_tenant_id: Annotated[str | None, Header()] = None,
        x_user_id: Annotated[str | None, Header()] = None,
    ) -> dict[str, object]:
        tenant_id, user_id = identity(x_tenant_id, x_user_id)
        result = container.memory_service.resolve(
            tenant_id=tenant_id,
            user_id=user_id,
            memory_id=memory_id,
            action=body.action,
            expected_revision=body.expected_revision,
            related_expected_revision=body.related_expected_revision,
        )
        return result.model_dump(mode="json")

    @app.delete("/api/v1/memories/{memory_id}")
    def forget_memory(
        memory_id: str,
        expected_revision: Annotated[int, Query(ge=1)],
        x_tenant_id: Annotated[str | None, Header()] = None,
        x_user_id: Annotated[str | None, Header()] = None,
    ) -> dict[str, object]:
        tenant_id, user_id = identity(x_tenant_id, x_user_id)
        result = container.memory_service.forget(
            tenant_id=tenant_id,
            user_id=user_id,
            memory_id=memory_id,
            expected_revision=expected_revision,
        )
        return result.model_dump(mode="json")

    @app.get("/api/v1/tasks/{task_id}/pending-writes/{write_id}")
    def get_pending_write(
        task_id: str,
        write_id: str,
        x_tenant_id: Annotated[str | None, Header()] = None,
        x_user_id: Annotated[str | None, Header()] = None,
    ) -> dict[str, object]:
        tenant_id, user_id = identity(x_tenant_id, x_user_id)
        return _pending_write_projection(container.pending_write_service.get(
            tenant_id, user_id, task_id, write_id
        ))

    @app.post(
        "/api/v1/tasks/{task_id}/pending-writes/{write_id}/approve",
        status_code=202,
    )
    def approve_pending_write(
        task_id: str,
        write_id: str,
        body: ApproveWriteRequest,
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
        x_tenant_id: Annotated[str | None, Header()] = None,
        x_user_id: Annotated[str | None, Header()] = None,
    ) -> dict[str, object]:
        tenant_id, user_id = identity(x_tenant_id, x_user_id)
        return _pending_write_projection(container.pending_write_service.approve(
            tenant_id=tenant_id,
            user_id=user_id,
            task_id=task_id,
            write_id=write_id,
            plan_hash=body.plan_hash,
            comment=body.comment,
            idempotency_key=idempotency_key,
        ))

    @app.post(
        "/api/v1/tasks/{task_id}/pending-writes/{write_id}/reject",
        status_code=202,
    )
    def reject_pending_write(
        task_id: str,
        write_id: str,
        body: RejectWriteRequest,
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
        x_tenant_id: Annotated[str | None, Header()] = None,
        x_user_id: Annotated[str | None, Header()] = None,
    ) -> dict[str, object]:
        tenant_id, user_id = identity(x_tenant_id, x_user_id)
        return _pending_write_projection(container.pending_write_service.reject(
            tenant_id=tenant_id,
            user_id=user_id,
            task_id=task_id,
            write_id=write_id,
            reason=body.reason,
            idempotency_key=idempotency_key,
        ))

    @app.get("/api/v1/tasks/{task_id}/events")
    async def task_events(
        task_id: str,
        after_id: int | None = None,
        last_event_id: Annotated[str | None, Header(alias="Last-Event-ID")] = None,
        x_tenant_id: Annotated[str | None, Header()] = None,
        x_user_id: Annotated[str | None, Header()] = None,
    ) -> StreamingResponse:
        tenant_id, user_id = identity(x_tenant_id, x_user_id)
        container.event_service.assert_task(tenant_id, user_id, task_id)
        try:
            cursor = after_id if after_id is not None else int(last_event_id or 0)
        except ValueError as exc:
            raise InvalidCursorError("Last-Event-ID must be an integer event cursor.") from exc
        if cursor < 0:
            raise InvalidCursorError("Event cursor must be non-negative.")
        container.event_service.validate_cursor(tenant_id, task_id, cursor)
        await container.event_service.acquire_connection(tenant_id, user_id, task_id)

        async def authorized_stream() -> AsyncIterator[str]:
            try:
                async for item in container.event_service.stream(
                    tenant_id, task_id, cursor
                ):
                    yield item
            finally:
                await container.event_service.release_connection(
                    tenant_id, user_id, task_id
                )

        return StreamingResponse(
            authorized_stream(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache, no-transform",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    @app.get("/api/v1/tasks/{task_id}/workspace")
    def list_workspace(
        task_id: str,
        prefix: str = "",
        glob: str | None = None,
        x_tenant_id: Annotated[str | None, Header()] = None,
        x_user_id: Annotated[str | None, Header()] = None,
    ) -> dict[str, object]:
        tenant_id, user_id = identity(x_tenant_id, x_user_id)
        container.task_service.get(tenant_id, user_id, task_id)
        try:
            files = container.workspace_service.list(tenant_id, task_id, prefix)
        except (FileNotFoundError, ValueError) as exc:
            raise NotFoundError("Workspace path was not found.") from exc
        if glob is not None:
            files = [item for item in files if fnmatch.fnmatchcase(item.workspace_path, glob)]
        return {
            "files": [
                item.model_dump(mode="json")
                for item in files
            ]
        }

    @app.post("/api/v1/tasks/{task_id}/data-files", status_code=201)
    def upload_data_file(
        task_id: str,
        file: Annotated[UploadFile, File()],
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
        x_tenant_id: Annotated[str | None, Header()] = None,
        x_user_id: Annotated[str | None, Header()] = None,
    ) -> JSONResponse:
        tenant_id, user_id = identity(x_tenant_id, x_user_id)
        container.task_service.get(tenant_id, user_id, task_id)
        size_bytes, digest = _hash_upload(
            file.file, container.settings.phase6_limits.data_upload_max_bytes
        )
        media_type, suffix = _data_media_stream(file.file, file.filename or "data")
        logical_id = hashlib.sha256(
            f"{tenant_id}|{user_id}|{task_id}|data-files|{idempotency_key}".encode()
        ).hexdigest()
        safe_name = re.sub(r"[^A-Za-z0-9._-]+", "_", file.filename or f"data.{suffix}")
        with container.session_factory() as session:
            existing = session.scalar(
                select(WorkspaceFileRow).where(
                    WorkspaceFileRow.tenant_id == tenant_id,
                    WorkspaceFileRow.task_id == task_id,
                    WorkspaceFileRow.operation_key == logical_id,
                )
            )
            if existing is not None and existing.status == "pending":
                raise IdempotencyInProgressError("Data file upload is already in progress.")
            replayed = existing is not None and existing.status == "ready"
            if replayed:
                assert existing is not None
                if (
                    existing.content_hash != digest
                    or existing.size != size_bytes
                    or existing.mime_type != media_type
                ):
                    raise IdempotencyConflictError(
                        "Idempotency-Key was replayed with a different data file."
                    )
        if replayed:
            assert existing is not None
            workspace_file = container.workspace_service.metadata(
                tenant_id, task_id, existing.workspace_path
            )
        else:
            file.file.seek(0)
            workspace_file = container.workspace_service.publish_stream(
                tenant_id=tenant_id,
                task_id=task_id,
                path=f"files/data/{logical_id[:24]}/{safe_name}",
                stream=file.file,
                size=size_bytes,
                content_hash=digest,
                mime_type=media_type,
                operation_key=logical_id,
            )
        container.dataset_service.bind(
            tenant_id=tenant_id,
            task_id=task_id,
            workspace_path=workspace_file.workspace_path,
            operation_key=hashlib.sha256(f"{logical_id}|dataset-bind".encode()).hexdigest(),
        )
        return JSONResponse(
            workspace_file.model_dump(mode="json"),
            status_code=200 if replayed else 201,
        )

    @app.get("/api/v1/tasks/{task_id}/workspace/{workspace_path:path}")
    def read_workspace(
        task_id: str,
        workspace_path: str,
        request: Request,
        download: bool = False,
        x_tenant_id: Annotated[str | None, Header()] = None,
        x_user_id: Annotated[str | None, Header()] = None,
    ) -> Response:
        tenant_id, user_id = identity(x_tenant_id, x_user_id)
        container.task_service.get(tenant_id, user_id, task_id)
        try:
            metadata = container.workspace_service.metadata(
                tenant_id, task_id, workspace_path
            )
            content = container.workspace_service.read(
                tenant_id, task_id, workspace_path
            )
        except (FileNotFoundError, ValueError) as exc:
            raise NotFoundError("Workspace file was not found.") from exc
        headers = {"Accept-Ranges": "bytes", "ETag": f'"{metadata.content_hash}"'}
        if download:
            headers["Content-Disposition"] = (
                f'attachment; filename="{workspace_path.rsplit("/", 1)[-1]}"'
            )
        status_code = 200
        range_header = request.headers.get("Range")
        if range_header:
            start, end = _parse_byte_range(range_header, len(content))
            content = content[start : end + 1]
            headers["Content-Range"] = f"bytes {start}-{end}/{metadata.size}"
            status_code = 206
        return Response(
            content,
            status_code=status_code,
            media_type=metadata.mime_type,
            headers=headers,
        )

    @app.post("/api/v1/model-configs", status_code=201)
    def create_model_config(body: CreateModelConfigRequest) -> dict[str, object]:
        require_admin()
        context = authenticated_context()
        row, head = model_config_admin.create(
            tenant_id=context.tenant_id,
            user_id=context.user_id,
            command=ModelConfigCreate(**body.model_dump()),
        )
        return _model_config_projection(row, head, revoked=False)

    @app.get("/api/v1/model-configs")
    def list_model_configs() -> dict[str, object]:
        require_admin()
        context = authenticated_context()
        with container.session_factory() as session:
            pairs = list(
                session.execute(
                    select(ModelConfigRow, ModelConfigHeadRow)
                    .join(
                        ModelConfigHeadRow,
                        (ModelConfigHeadRow.tenant_id == ModelConfigRow.tenant_id)
                        & (
                            ModelConfigHeadRow.model_config_id
                            == ModelConfigRow.model_config_id
                        )
                        & (
                            ModelConfigHeadRow.current_version
                            == ModelConfigRow.version
                        ),
                    )
                    .where(ModelConfigRow.tenant_id == context.tenant_id)
                    .order_by(ModelConfigRow.model_config_id)
                ).all()
            )
            revoked = {
                (item.model_config_id, item.version)
                for item in session.scalars(
                    select(ModelConfigRevocationRow).where(
                        ModelConfigRevocationRow.tenant_id == context.tenant_id
                    )
                ).all()
            }
        items = [
            _model_config_projection(
                row,
                head,
                revoked=(row.model_config_id, row.version) in revoked,
            )
            for row, head in pairs
        ]
        return {"items": items, "page": {"limit": len(items), "has_more": False}}

    @app.get("/api/v1/model-configs/{model_config_id}/versions/{version}")
    def get_model_config_revision(
        model_config_id: str, version: int
    ) -> dict[str, object]:
        require_admin()
        context = authenticated_context()
        with container.session_factory() as session:
            row = session.scalar(
                select(ModelConfigRow).where(
                    ModelConfigRow.tenant_id == context.tenant_id,
                    ModelConfigRow.model_config_id == model_config_id,
                    ModelConfigRow.version == version,
                )
            )
            head = session.get(
                ModelConfigHeadRow, (context.tenant_id, model_config_id)
            )
            revoked = session.scalar(
                select(ModelConfigRevocationRow).where(
                    ModelConfigRevocationRow.tenant_id == context.tenant_id,
                    ModelConfigRevocationRow.model_config_id == model_config_id,
                    ModelConfigRevocationRow.version == version,
                )
            )
        if row is None or head is None:
            raise NotFoundError("ModelConfig revision was not found.")
        return _model_config_projection(row, head, revoked=revoked is not None)

    @app.patch("/api/v1/model-configs/{model_config_id}")
    def patch_model_config(
        model_config_id: str, body: PatchModelConfigRequest
    ) -> dict[str, object]:
        require_admin()
        context = authenticated_context()
        row, head = model_config_admin.update(
            tenant_id=context.tenant_id,
            user_id=context.user_id,
            model_config_id=model_config_id,
            command=ModelConfigPatch(**body.model_dump()),
        )
        return _model_config_projection(row, head, revoked=False)

    @app.post("/api/v1/model-configs/{model_config_id}/test")
    def test_model_config(model_config_id: str) -> dict[str, str]:
        require_admin()
        context = authenticated_context()
        with container.session_factory() as session:
            head = session.get(
                ModelConfigHeadRow, (context.tenant_id, model_config_id)
            )
            row = (
                session.get(ModelConfigRow, (model_config_id, head.current_version))
                if head is not None
                else None
            )
            if row is not None and row.tenant_id != context.tenant_id:
                row = None
            if row is not None:
                session.expunge(row)
        if row is None:
            raise NotFoundError("ModelConfig was not found.")
        test_model_revision(
            runtime=cast(ModelRuntimeForAdmin, container.model_runtime),
            tenant_id=context.tenant_id,
            row=row,
        )
        return {"status": "ok"}

    @app.post("/api/v1/model-configs/{model_config_id}/disable")
    def disable_model_config(model_config_id: str) -> dict[str, object]:
        require_admin()
        context = authenticated_context()
        head = model_config_admin.disable(
            tenant_id=context.tenant_id,
            user_id=context.user_id,
            model_config_id=model_config_id,
        )
        with container.session_factory() as session:
            row = session.get(
                ModelConfigRow, (model_config_id, head.current_version)
            )
            if row is not None and row.tenant_id != context.tenant_id:
                row = None
        if row is None:
            raise NotFoundError("ModelConfig was not found.")
        return _model_config_projection(row, head, revoked=False)

    @app.post(
        "/api/v1/model-configs/{model_config_id}/versions/{version}/revoke"
    )
    def revoke_model_config(
        model_config_id: str,
        version: int,
        body: RevokeModelConfigRequest,
    ) -> dict[str, object]:
        require_admin()
        context = authenticated_context()
        row = model_config_admin.revoke(
            tenant_id=context.tenant_id,
            user_id=context.user_id,
            model_config_id=model_config_id,
            version=version,
            reason=body.reason,
        )
        return {
            "model_config_id": row.model_config_id,
            "version": row.version,
            "revoked_at": row.revoked_at.isoformat(),
            "reason": row.reason,
        }

    @app.put("/api/v1/model-bindings/{binding_key}", status_code=204)
    def replace_model_binding(
        binding_key: Literal[
            "routing",
            "reasoning",
            "tool",
            "data",
            "code",
            "writing",
            "embedding",
            "rerank",
        ],
        body: ReplaceModelBindingRequest,
    ) -> Response:
        require_admin()
        context = authenticated_context()
        model_config_admin.replace_binding(
            tenant_id=context.tenant_id,
            user_id=context.user_id,
            binding_key=binding_key,
            model_config_ids=body.model_config_ids,
        )
        return Response(status_code=204)

    @app.post("/api/v1/datasources", status_code=201)
    def create_datasource(body: CreateDataSourceRequest) -> dict[str, object]:
        require_admin()
        context = authenticated_context()
        row = datasource_admin.create(
            tenant_id=context.tenant_id,
            user_id=context.user_id,
            command=DataSourceCreate(**body.model_dump()),
        )
        return _datasource_projection(container, row, None, [], admin=True)

    @app.patch("/api/v1/datasources/{datasource_id}")
    def patch_datasource(
        datasource_id: str, body: PatchDataSourceRequest
    ) -> dict[str, object]:
        require_admin()
        context = authenticated_context()
        row = datasource_admin.update(
            tenant_id=context.tenant_id,
            user_id=context.user_id,
            datasource_id=datasource_id,
            command=DataSourcePatch(**body.model_dump()),
        )
        return _datasource_projection(container, row, None, [], admin=True)

    @app.post("/api/v1/datasources/{datasource_id}/test")
    def test_datasource(datasource_id: str) -> dict[str, str]:
        require_admin()
        context = authenticated_context()
        datasource_admin.test(
            tenant_id=context.tenant_id, datasource_id=datasource_id
        )
        return {"status": "ok"}

    @app.post("/api/v1/datasources/{datasource_id}/enable")
    def enable_datasource(datasource_id: str) -> dict[str, object]:
        require_admin()
        context = authenticated_context()
        row = datasource_admin.set_enabled(
            tenant_id=context.tenant_id,
            user_id=context.user_id,
            datasource_id=datasource_id,
            enabled=True,
        )
        return _datasource_projection(container, row, None, [], admin=True)

    @app.post("/api/v1/datasources/{datasource_id}/disable")
    def disable_datasource(datasource_id: str) -> dict[str, object]:
        require_admin()
        context = authenticated_context()
        row = datasource_admin.set_enabled(
            tenant_id=context.tenant_id,
            user_id=context.user_id,
            datasource_id=datasource_id,
            enabled=False,
        )
        return _datasource_projection(container, row, None, [], admin=True)

    @app.put("/api/v1/datasources/{datasource_id}/policies", status_code=204)
    def replace_datasource_policies(
        datasource_id: str, body: ReplaceDataSourcePoliciesRequest
    ) -> Response:
        require_admin()
        context = authenticated_context()
        datasource_admin.replace_policies(
            tenant_id=context.tenant_id,
            user_id=context.user_id,
            datasource_id=datasource_id,
            policies=[
                TablePolicyInput(**item.model_dump()) for item in body.policies
            ],
        )
        return Response(status_code=204)

    @app.put(
        "/api/v1/datasources/{datasource_id}/grants/{user_id}",
        status_code=204,
    )
    def put_datasource_grant(
        datasource_id: str,
        user_id: str,
        body: PutDataSourceGrantRequest,
    ) -> Response:
        require_admin()
        context = authenticated_context()
        datasource_admin.put_grant(
            tenant_id=context.tenant_id,
            actor_user_id=context.user_id,
            datasource_id=datasource_id,
            grantee_user_id=user_id,
            access_level=body.access_level,
        )
        return Response(status_code=204)

    @app.delete(
        "/api/v1/datasources/{datasource_id}/grants/{user_id}",
        status_code=204,
    )
    def delete_datasource_grant(datasource_id: str, user_id: str) -> Response:
        require_admin()
        context = authenticated_context()
        datasource_admin.delete_grant(
            tenant_id=context.tenant_id,
            actor_user_id=context.user_id,
            datasource_id=datasource_id,
            grantee_user_id=user_id,
        )
        return Response(status_code=204)

    @app.get("/api/v1/datasources")
    def list_datasources() -> dict[str, object]:
        context = authenticated_context()
        with container.session_factory() as session:
            if context.is_admin:
                sources = list(
                    session.scalars(
                        select(DataSourceRow)
                        .where(DataSourceRow.tenant_id == context.tenant_id)
                        .order_by(DataSourceRow.name, DataSourceRow.datasource_id)
                    ).all()
                )
            else:
                sources = list(
                    session.scalars(
                        select(DataSourceRow)
                        .join(
                            DataSourceUserGrantRow,
                            (DataSourceUserGrantRow.tenant_id == DataSourceRow.tenant_id)
                            & (
                                DataSourceUserGrantRow.datasource_id
                                == DataSourceRow.datasource_id
                            ),
                        )
                        .where(
                            DataSourceRow.tenant_id == context.tenant_id,
                            DataSourceUserGrantRow.user_id == context.user_id,
                        )
                        .order_by(DataSourceRow.name, DataSourceRow.datasource_id)
                    ).all()
                )
            grants = {
                item.datasource_id: item
                for item in session.scalars(
                    select(DataSourceUserGrantRow).where(
                        DataSourceUserGrantRow.tenant_id == context.tenant_id,
                        DataSourceUserGrantRow.user_id == context.user_id,
                    )
                ).all()
            }
            policies = list(
                session.scalars(
                    select(DataSourceTablePolicyRow).where(
                        DataSourceTablePolicyRow.tenant_id == context.tenant_id
                    )
                ).all()
            )
        projected = [
            _datasource_projection(
                container,
                row,
                grants.get(row.datasource_id),
                [item for item in policies if item.datasource_id == row.datasource_id],
                admin=context.is_admin,
            )
            for row in sources
        ]
        return {
            "items": projected,
            "page": {"limit": len(projected), "has_more": False},
        }

    @app.get("/api/v1/datasources/{datasource_id}")
    def get_datasource(datasource_id: str) -> dict[str, object]:
        items = list_datasources()["items"]
        assert isinstance(items, list)
        for item in items:
            if isinstance(item, dict) and item.get("datasource_id") == datasource_id:
                return item
        raise NotFoundError("DataSource was not found.")

    @app.post("/api/v1/documents", status_code=201)
    def upload_document(
        file: Annotated[UploadFile, File()],
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
        name: str | None = None,
        x_tenant_id: Annotated[str | None, Header()] = None,
        x_user_id: Annotated[str | None, Header()] = None,
    ) -> JSONResponse:
        require_admin()
        tenant_id, user_id = identity(x_tenant_id, x_user_id)
        size_bytes, digest = _hash_upload(file.file, 100 * 1024**2)
        media_type = _document_media_stream(file.file, file.filename or "document")
        with container.session_factory() as session, session.begin():
            reservation = session.get(
                DocumentUploadReservationRow, (tenant_id, idempotency_key)
            )
            if reservation is not None:
                expires_at = reservation.expires_at
                now = utcnow()
                if expires_at.tzinfo is None:
                    expires_at = expires_at.replace(tzinfo=now.tzinfo)
                if expires_at <= now:
                    session.delete(reservation)
                    session.flush()
                    reservation = None
            if reservation is not None:
                if reservation.content_hash is None:
                    raise IdempotencyInProgressError(
                        "Document upload is already in progress."
                    )
                if reservation.content_hash != digest:
                    raise IdempotencyConflictError(
                        "Idempotency-Key was replayed with a different document."
                    )
                doc_id = reservation.doc_id
                replayed = True
            else:
                doc_id = new_id("doc")
                session.add(
                    DocumentUploadReservationRow(
                        tenant_id=tenant_id,
                        idempotency_key=idempotency_key,
                        doc_id=doc_id,
                        expires_at=utcnow() + timedelta(hours=24),
                    )
                )
                replayed = False
        if not replayed:
            file.file.seek(0)
            container.document_service.create_stream(
                tenant_id=tenant_id,
                name=name or file.filename or "document",
                media_type=media_type,
                stream=file.file,
                size_bytes=size_bytes,
                content_hash=digest,
                doc_id=doc_id,
                idempotency_key=idempotency_key,
            )
        row = container.document_service.get(tenant_id, doc_id)
        if not replayed:
            container.audit_service.append(
                tenant_id=tenant_id,
                actor_ref=container.audit_service.user_actor(user_id),
                action="document.upload",
                resource_ref=f"document:{doc_id}",
                result="succeeded",
                trace_id=current_trace_id(),
            )
        return JSONResponse(
            _document_projection(row),
            status_code=200 if replayed else 201,
            headers={"Location": f"/api/v1/documents/{doc_id}"},
        )

    @app.get("/api/v1/documents")
    def list_documents(
        status: Literal["pending", "ready", "failed"] | None = None,
        tags: Annotated[list[str] | None, Query()] = None,
        cursor: str | None = None,
        limit: int = 50,
        x_tenant_id: Annotated[str | None, Header()] = None,
        x_user_id: Annotated[str | None, Header()] = None,
    ) -> dict[str, object]:
        tenant_id, _ = identity(x_tenant_id, x_user_id)
        if limit < 1 or limit > 100:
            raise InvalidCursorError("Document page limit must be between 1 and 100.")
        with container.session_factory() as session:
            statement = select(DocumentRow).where(
                DocumentRow.tenant_id == tenant_id,
                DocumentRow.deleted_at.is_(None),
            )
            if status:
                statement = statement.where(DocumentRow.status == status)
            rows = list(
                session.scalars(
                    statement.order_by(DocumentRow.updated_at.desc(), DocumentRow.doc_id)
                ).all()
            )
        if tags:
            required = set(tags)
            rows = [row for row in rows if required.issubset(set(row.tags or []))]
        scope = {"tenant_id": tenant_id}
        filters = {"status": status, "tags": sorted(tags or [])}
        if cursor:
            last_key = cursor_codec.decode(
                cursor,
                resource="documents",
                scope=scope,
                filters=filters,
                sort="updated_at_desc",
            )
            if not isinstance(last_key, list) or len(last_key) != 2:
                raise InvalidCursorError("Document cursor is malformed.")
            updated_at, doc_id = str(last_key[0]), str(last_key[1])
            try:
                start = next(
                    index + 1
                    for index, row in enumerate(rows)
                    if row.updated_at.isoformat() == updated_at and row.doc_id == doc_id
                )
            except StopIteration as exc:
                raise InvalidCursorError("Document cursor is no longer valid.") from exc
        else:
            start = 0
        selected = rows[start : start + limit]
        next_cursor = (
            cursor_codec.encode(
                resource="documents",
                scope=scope,
                filters=filters,
                sort="updated_at_desc",
                last_key=[selected[-1].updated_at.isoformat(), selected[-1].doc_id],
            )
            if start + len(selected) < len(rows) and selected
            else None
        )
        items = [_document_projection(row) for row in selected]
        return {
            "items": items,
            "page": {
                "limit": limit,
                "has_more": next_cursor is not None,
                "next_cursor": next_cursor,
            },
            # Kept for clients created before the common page envelope was finalized.
            "documents": items,
            "next_cursor": next_cursor,
        }

    @app.get("/api/v1/documents/{doc_id}")
    def get_document(
        doc_id: str,
        x_tenant_id: Annotated[str | None, Header()] = None,
        x_user_id: Annotated[str | None, Header()] = None,
    ) -> dict[str, object]:
        tenant_id, _ = identity(x_tenant_id, x_user_id)
        try:
            return _document_projection(container.document_service.get(tenant_id, doc_id))
        except FileNotFoundError as exc:
            raise NotFoundError("Document was not found.") from exc

    @app.get("/api/v1/documents/{doc_id}/content")
    def get_document_content(
        doc_id: str,
        disposition: Literal["inline", "attachment"] = "inline",
        x_tenant_id: Annotated[str | None, Header()] = None,
        x_user_id: Annotated[str | None, Header()] = None,
    ) -> Response:
        tenant_id, _ = identity(x_tenant_id, x_user_id)
        try:
            document, content = container.document_service.read_original(tenant_id, doc_id)
        except FileNotFoundError as exc:
            raise NotFoundError("Document was not found.") from exc
        headers = {
            "Content-Disposition": f'{disposition}; filename="{_safe_header_filename(document.name)}"',
            "ETag": f'"{document.content_hash}"',
        }
        return Response(content, media_type=document.media_type, headers=headers)

    @app.delete("/api/v1/documents/{doc_id}", status_code=204)
    def delete_document(
        doc_id: str,
        x_tenant_id: Annotated[str | None, Header()] = None,
        x_user_id: Annotated[str | None, Header()] = None,
    ) -> Response:
        require_admin()
        tenant_id, user_id = identity(x_tenant_id, x_user_id)
        try:
            container.document_service.delete(tenant_id, doc_id)
        except FileNotFoundError as exc:
            raise NotFoundError("Document was not found.") from exc
        container.audit_service.append(
            tenant_id=tenant_id,
            actor_ref=container.audit_service.user_actor(user_id),
            action="document.delete",
            resource_ref=f"document:{doc_id}",
            result="succeeded",
            trace_id=current_trace_id(),
        )
        return Response(status_code=204)

    @app.get("/api/v1/documents/{doc_id}/ingestion")
    def get_document_ingestion(
        doc_id: str,
        x_tenant_id: Annotated[str | None, Header()] = None,
        x_user_id: Annotated[str | None, Header()] = None,
    ) -> dict[str, object]:
        tenant_id, _ = identity(x_tenant_id, x_user_id)
        try:
            doc = container.document_service.get(tenant_id, doc_id)
            ingestion = container.document_service.ingestion(tenant_id, doc_id)
        except FileNotFoundError as exc:
            raise NotFoundError("Document was not found.") from exc
        return {
            "doc_id": doc_id,
            "document_status": doc.status,
            "published_generation": doc.published_generation,
            "ingestion": {
                "generation": ingestion.generation,
                "status": ingestion.status,
                "stage": ingestion.stage,
                "attempt_count": ingestion.attempt_count,
                "error_code": ingestion.error_code,
                "updated_at": ingestion.updated_at.isoformat(),
            },
            "updated_at": doc.updated_at.isoformat(),
        }

    @app.post("/api/v1/documents/{doc_id}/ingestion/retry", status_code=202)
    def retry_document_ingestion(
        doc_id: str,
        x_tenant_id: Annotated[str | None, Header()] = None,
        x_user_id: Annotated[str | None, Header()] = None,
    ) -> dict[str, object]:
        require_admin()
        tenant_id, _ = identity(x_tenant_id, x_user_id)
        container.document_service.get(tenant_id, doc_id)
        with container.session_factory() as session, session.begin():
            row = session.get(DocumentIngestionRow, doc_id)
            if row is None or row.status != "failed":
                raise InvalidStateError("Only a failed ingestion can be retried.")
            row.status = "queued"
            row.error_code = None
            row.updated_at = utcnow()
            session.add(
                OutboxMessageRow(
                    message_id=new_id("outbox"),
                    tenant_id=tenant_id,
                    kind="document.ingest",
                    aggregate_id=doc_id,
                    payload_json={"doc_id": doc_id},
                    idempotency_key=f"document:{doc_id}:generation:{row.generation}:attempt:{row.attempt_count + 1}",
                    available_at=utcnow(),
                )
            )
        return {"doc_id": doc_id, "status": "queued"}

    @app.post("/api/v1/documents/{doc_id}/ingestion/rebuild", status_code=202)
    def rebuild_document(
        doc_id: str,
        x_tenant_id: Annotated[str | None, Header()] = None,
        x_user_id: Annotated[str | None, Header()] = None,
    ) -> dict[str, object]:
        require_admin()
        tenant_id, _ = identity(x_tenant_id, x_user_id)
        generation = container.document_service.queue_rebuild(tenant_id, doc_id)
        with container.session_factory() as session, session.begin():
            session.add(
                OutboxMessageRow(
                    message_id=new_id("outbox"),
                    tenant_id=tenant_id,
                    kind="document.ingest",
                    aggregate_id=doc_id,
                    payload_json={"doc_id": doc_id},
                    idempotency_key=f"document:{doc_id}:generation:{generation}",
                    available_at=utcnow(),
                )
            )
        return {"doc_id": doc_id, "generation": generation, "status": "queued"}

    @app.get("/healthz")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/readyz")
    def readiness(request: Request) -> Response:
        try:
            with container.session_factory() as session:
                session.execute(select(1)).scalar_one()
                dialect = session.get_bind().dialect.name
                revision = (
                    session.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
                    if dialect == "postgresql"
                    else "metadata"
                )
            if dialect == "postgresql" and revision != SCHEMA_HEAD:
                raise RuntimeError("schema revision mismatch")
        except Exception:
            return problem(
                request,
                status=503,
                code="dependency_unavailable",
                title="Dependency unavailable",
                detail="PostgreSQL or the required schema revision is not ready.",
                retryable=True,
            )
        return JSONResponse(
            {"status": "ready", "database": "ready", "schema_revision": revision}
        )

    # Added last so CORS is the outermost middleware. Browser preflight requests
    # must be answered before the authenticated /api/v1 request middleware.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(container.settings.auth_allowed_origins),
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=[
            "Authorization",
            "Content-Type",
            "Idempotency-Key",
            "Last-Event-ID",
            "X-CSRF-Token",
            "X-Request-ID",
        ],
        expose_headers=["Content-Disposition", "Content-Range", "ETag", "X-Request-ID"],
    )
    return app


app = create_app()


def _data_media_stream(stream: BinaryIO, filename: str) -> tuple[str, str]:
    lower = filename.lower()
    stream.seek(0)
    header = stream.read(4)
    stream.seek(0, 2)
    length = stream.tell()
    if length >= 4:
        stream.seek(-4, 2)
        footer = stream.read(4)
    else:
        footer = b""
    stream.seek(0)
    if header == b"PAR1" and footer == b"PAR1":
        return "application/vnd.apache.parquet", "parquet"
    if header[:2] == b"PK" and lower.endswith(".xlsx"):
        return "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", "xlsx"
    decoder = codecs.getincrementaldecoder("utf-8")()
    try:
        while chunk := stream.read(1024 * 1024):
            decoder.decode(chunk)
        decoder.decode(b"", final=True)
    except UnicodeDecodeError as exc:
        raise UnsupportedMediaError("Unsupported or malformed data file.") from exc
    finally:
        stream.seek(0)
    if lower.endswith(".csv"):
        return "text/csv", "csv"
    raise UnsupportedMediaError("Only CSV, XLSX and Parquet are accepted.")


def _hash_upload(stream: BinaryIO, limit: int) -> tuple[int, str]:
    stream.seek(0)
    digest = hashlib.sha256()
    size = 0
    while chunk := stream.read(1024 * 1024):
        size += len(chunk)
        if size > limit:
            stream.seek(0)
            raise UploadTooLargeError(f"Upload exceeds {limit // 1024**2} MiB.")
        digest.update(chunk)
    stream.seek(0)
    return size, digest.hexdigest()


def _document_media_stream(stream: BinaryIO, filename: str) -> str:
    lower = filename.lower()
    stream.seek(0)
    header = stream.read(8)
    stream.seek(0)
    if header.startswith(b"%PDF-"):
        return "application/pdf"
    if header[:2] == b"PK":
        try:
            with zipfile.ZipFile(stream) as archive:
                if "word/document.xml" in archive.namelist():
                    stream.seek(0)
                    return "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        except zipfile.BadZipFile:
            pass
        stream.seek(0)
        raise UnsupportedMediaError("Unsupported ZIP-based document media.")
    decoder = codecs.getincrementaldecoder("utf-8")()
    preview: list[str] = []
    try:
        while chunk := stream.read(1024 * 1024):
            text = decoder.decode(chunk)
            if sum(len(item) for item in preview) < 64 * 1024:
                preview.append(text[: 64 * 1024])
        decoder.decode(b"", final=True)
    except UnicodeDecodeError as exc:
        stream.seek(0)
        raise UnsupportedMediaError("Unsupported document media.") from exc
    finally:
        stream.seek(0)
    text = "".join(preview)[: 64 * 1024]
    if re.search(r"<html[\s>]", text, re.IGNORECASE):
        return "text/html"
    return "text/markdown" if lower.endswith(".md") else "text/plain"


def _document_projection(row: DocumentRow) -> dict[str, object]:
    return {
        "doc_id": row.doc_id,
        "name": row.name,
        "media_type": row.media_type,
        "size_bytes": row.size_bytes,
        "content_hash": row.content_hash,
        "status": row.status,
        "tags": row.tags or [],
        "published_generation": row.published_generation,
        "created_at": row.created_at.isoformat(),
        "updated_at": row.updated_at.isoformat(),
    }


def _safe_header_filename(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("._") or "document"


def _thread_projection(row: ThreadRow) -> dict[str, object]:
    return {
        "thread_id": row.thread_id,
        "created_at": row.created_at.isoformat(),
        "updated_at": row.updated_at.isoformat(),
    }


def _row_start(rows: list[Any], last_key: object, identity_field: str) -> int:
    if last_key is None:
        return 0
    if not isinstance(last_key, list) or len(last_key) != 2:
        raise InvalidCursorError("Cursor position is malformed.")
    timestamp, identity_value = str(last_key[0]), str(last_key[1])
    for index, row in enumerate(rows):
        row_timestamp = getattr(row, "updated_at", None) or getattr(row, "created_at")
        if (
            row_timestamp.isoformat() == timestamp
            and str(getattr(row, identity_field)) == identity_value
        ):
            return index + 1
    raise InvalidCursorError("Cursor position is no longer valid.")


def _owned_task_state(
    container: Container,
    tenant_id: str,
    user_id: str,
    task_id: str,
) -> ResearchState:
    container.task_service.get(tenant_id, user_id, task_id)
    with container.session_factory() as session:
        row = session.scalar(
            select(ResearchTaskRow).where(
                ResearchTaskRow.tenant_id == tenant_id,
                ResearchTaskRow.user_id == user_id,
                ResearchTaskRow.task_id == task_id,
                ResearchTaskRow.deleted_at.is_(None),
            )
        )
        if row is None:
            raise NotFoundError("Task was not found.")
        return ResearchState.model_validate(row.latest_state)


def _pending_write_projection(value: Any) -> dict[str, object]:
    payload = value.model_dump(
        mode="json",
        exclude={"tenant_id", "user_id", "operation_key"},
        exclude_none=True,
    )
    result = payload.get("result")
    if isinstance(result, dict):
        result.pop("operation_key", None)
    return payload


def _datasource_projection(
    container: Container,
    row: DataSourceRow,
    grant: DataSourceUserGrantRow | None,
    policies: list[DataSourceTablePolicyRow],
    *,
    admin: bool = False,
) -> dict[str, object]:
    policy_allows_write = any(
        policy.allow_insert or policy.allow_update or policy.allow_delete
        for policy in policies
    )
    write_principal = False
    if row.enabled and grant is not None and grant.access_level == "write":
        try:
            resolve_database_credential(
                container.datasource_service.credentials,
                tenant_id=row.tenant_id,
                credential_ref=row.credential_ref,
                mode="write",
            )
            write_principal = True
        except (KeyError, PermissionError, TypeError, ValueError):
            write_principal = False
    projection: dict[str, object] = {
        "datasource_id": row.datasource_id,
        "name": row.name,
        "enabled": row.enabled,
        "access_level": grant.access_level if grant is not None else None,
        "write_ready": bool(
            row.enabled
            and grant is not None
            and grant.access_level == "write"
            and policy_allows_write
            and write_principal
        ),
    }
    if admin:
        projection.update(
            {
                "host": row.host,
                "port": row.port,
                "database_name": row.database_name,
                "ssl_mode": row.ssl_mode,
                "credential_configured": bool(row.credential_ref),
                "created_at": row.created_at.isoformat(),
                "updated_at": row.updated_at.isoformat(),
            }
        )
    return projection


def _model_config_projection(
    row: ModelConfigRow,
    head: ModelConfigHeadRow,
    *,
    revoked: bool,
) -> dict[str, object]:
    return {
        "model_config_id": row.model_config_id,
        "version": row.version,
        "provider": row.provider,
        "model": row.model,
        "base_url": row.base_url,
        "purpose": row.purpose,
        "secret_configured": bool(row.secret_ref),
        "enabled_for_new_tasks": head.enabled_for_new_tasks,
        "revoked": revoked,
        "created_at": row.created_at.isoformat(),
        "updated_at": head.updated_at.isoformat(),
    }


def _parse_byte_range(value: str, size: int) -> tuple[int, int]:
    match = re.fullmatch(r"bytes=(\d*)-(\d*)", value.strip())
    if match is None or size <= 0:
        raise RangeNotSatisfiableError("The requested byte range is invalid.")
    start_text, end_text = match.groups()
    if not start_text and not end_text:
        raise RangeNotSatisfiableError("The requested byte range is invalid.")
    if not start_text:
        suffix = int(end_text)
        if suffix <= 0:
            raise RangeNotSatisfiableError("The requested byte range is invalid.")
        start = max(0, size - suffix)
        end = size - 1
    else:
        start = int(start_text)
        end = int(end_text) if end_text else size - 1
    if start >= size or end < start:
        raise RangeNotSatisfiableError("The requested byte range is outside the file.")
    return start, min(end, size - 1)
