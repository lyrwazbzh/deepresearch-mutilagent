from __future__ import annotations

import os
from dataclasses import dataclass, field

from dotenv import load_dotenv


PLACEHOLDER_SECRETS = frozenset({"xxxx", "xxxxxx"})


class MissingRealProviderSecretError(RuntimeError):
    pass


@dataclass(frozen=True)
class Phase6Limits:
    readonly_sql_default_rows: int = 200
    readonly_sql_max_rows: int = 1_000
    readonly_sql_default_timeout_ms: int = 10_000
    readonly_sql_max_timeout_ms: int = 30_000
    dataset_query_default_rows: int = 100
    dataset_query_max_rows: int = 1_000
    dataset_profile_default_columns: int = 50
    dataset_profile_max_columns: int = 100
    data_upload_max_bytes: int = 256 * 1024**2
    sandbox_cpus: float = 2.0
    sandbox_memory_bytes: int = 2 * 1024**3
    sandbox_writable_disk_bytes: int = 2 * 1024**3
    sandbox_pids: int = 64
    sandbox_default_timeout_ms: int = 120_000
    sandbox_max_timeout_ms: int = 300_000
    sandbox_log_bytes: int = 4 * 1024**2
    sandbox_single_output_bytes: int = 256 * 1024**2
    sandbox_total_output_bytes: int = 512 * 1024**2
    chart_recommended_points: int = 5_000
    chart_max_points: int = 20_000
    chart_max_series: int = 20
    chart_max_categories: int = 200
    table_default_rows: int = 100
    table_max_rows: int = 500
    table_max_columns: int = 30
    table_cell_max_bytes: int = 4 * 1024
    table_serialized_max_bytes: int = 256 * 1024

    def __post_init__(self) -> None:
        positive = {
            name: value
            for name, value in self.__dict__.items()
            if not isinstance(value, bool)
        }
        if any(value <= 0 for value in positive.values()):
            raise ValueError("Phase 6 limits must all be positive")
        ordered = (
            (self.readonly_sql_default_rows, self.readonly_sql_max_rows),
            (
                self.readonly_sql_default_timeout_ms,
                self.readonly_sql_max_timeout_ms,
            ),
            (self.dataset_query_default_rows, self.dataset_query_max_rows),
            (
                self.dataset_profile_default_columns,
                self.dataset_profile_max_columns,
            ),
            (self.sandbox_default_timeout_ms, self.sandbox_max_timeout_ms),
            (self.chart_recommended_points, self.chart_max_points),
            (self.table_default_rows, self.table_max_rows),
            (self.sandbox_single_output_bytes, self.sandbox_total_output_bytes),
            (self.sandbox_total_output_bytes, self.sandbox_writable_disk_bytes),
        )
        if any(default > maximum for default, maximum in ordered):
            raise ValueError("Phase 6 default limits must not exceed hard limits")


DEFAULT_PHASE6_LIMITS = Phase6Limits()


@dataclass(frozen=True)
class Settings:
    database_url: str
    redis_url: str
    celery_broker_url: str
    default_tenant_id: str
    default_user_id: str
    model_mode: str
    lease_seconds: int = 300
    tenant_max_running_tasks: int = 4
    user_max_running_tasks: int = 2
    agent_max_steps: int = 6
    retrieval_branch_concurrency: int = 3
    phase2_enabled: bool = False
    phase3_enabled: bool = False
    phase4_enabled: bool = True
    phase5_enabled: bool = False
    phase9_enabled: bool = False
    minio_endpoint: str | None = None
    minio_access_key: str = "minioadmin"
    minio_secret_key: str = "minioadmin"
    minio_secure: bool = False
    elasticsearch_url: str | None = None
    elasticsearch_document_index: str = "document_chunks_v1"
    elasticsearch_text_analyzer: str = "ik_max_word"
    elasticsearch_search_analyzer: str = "ik_smart"
    tool_inline_budget: int = 32_000
    sandbox_image: str = "python:3.12.7-slim-bookworm"
    sandbox_runtime: str = "runsc"
    tenant_workspace_bytes_max: int = 20 * 1024**3
    task_workspace_bytes_max: int = 1 * 1024**3
    workspace_file_max_bytes: int = 256 * 1024**2
    query_completion_message_limit: int = 12
    query_completion_token_budget: int = 4_000
    ltm_candidate_k: int = 20
    ltm_top_k: int = 8
    ltm_min_similarity: float = 0.55
    ltm_min_lexical_score: float = 0.20
    ltm_token_budget: int = 1_200
    ltm_write_confidence_threshold: float = 0.70
    ltm_max_content_chars: int = 500
    openai_chat_provider: str = "openai"
    openai_base_url: str = "https://api.openai.com/v1"
    openai_chat_model: str = "gpt-4.1-mini"
    openai_embedding_provider: str = "openai"
    openai_embedding_base_url: str = "https://api.openai.com/v1"
    openai_embedding_model: str = "text-embedding-3-small"
    openai_embedding_dimensions: int = 1024
    openai_secret_ref: str = "OPENAI_API_KEY"
    openai_embedding_secret_ref: str = "OPENAI_EMBEDDING_API_KEY"
    tavily_secret_ref: str = "TAVILY_API_KEY"
    jina_base_url: str = "https://api.jina.ai/v1"
    jina_rerank_model: str = "jina-reranker-v2-base-multilingual"
    jina_secret_ref: str = "JINA_API_KEY"
    phase6_limits: Phase6Limits = field(default_factory=Phase6Limits)
    datasource_egress_allowlist: tuple[str, ...] = ("localhost:5432",)
    external_write_ledger_schema: str = "public"
    external_write_ledger_table: str = "agent_write_idempotency"
    neo4j_url: str | None = None
    neo4j_username: str = "neo4j"
    neo4j_password: str = "deepresearch"
    neo4j_database: str = "neo4j"
    auth_enabled: bool = False
    auth_jwt_algorithm: str = "RS256"
    auth_jwt_key_id: str = "phase10-dev"
    auth_jwt_issuer: str = "deepresearch-mutilagent"
    auth_jwt_audience: str = "deepresearch-api"
    auth_jwt_private_key: str | None = None
    auth_access_ttl_seconds: int = 900
    auth_refresh_ttl_days: int = 30
    auth_refresh_cookie_name: str = "dr_refresh"
    auth_refresh_cookie_path: str = "/api/v1/auth"
    auth_cookie_secure: bool = True
    auth_cookie_samesite: str = "lax"
    auth_allowed_origins: tuple[str, ...] = (
        "https://localhost",
        "http://localhost:3000",
        "http://localhost:5173",
    )
    cursor_signing_key: str = "phase10-development-cursor-key"
    cursor_ttl_seconds: int = 1_800
    task_event_retention_days: int = 7
    sse_heartbeat_seconds: int = 15
    sse_connections_per_task_max: int = 5
    sse_connections_per_user_max: int = 20
    sse_client_buffer_events_max: int = 256
    sse_client_buffer_bytes_max: int = 1_048_576
    telemetry_enabled: bool = False
    telemetry_max_queue_size: int = 2_048
    telemetry_max_export_batch_size: int = 256
    telemetry_export_delay_millis: int = 1_000
    trace_retention_days: int = 30
    audit_retention_days: int = 365
    share_expires_default_days: int = 30
    share_expires_max_days: int = 90
    share_public_base_url: str = ""
    public_share_rate_limit_requests: int = 60
    public_share_rate_limit_window_seconds: int = 60
    export_expires_days: int = 7
    export_max_attempts: int = 3
    export_execution_timeout_seconds: int = 900

    @classmethod
    def from_env(cls) -> "Settings":
        # Local development uses the ignored project .env. Existing process-level
        # variables keep precedence so production secret injection is not overwritten.
        load_dotenv(override=False)
        return cls(
            database_url=os.getenv(
                "DATABASE_URL",
                "postgresql+psycopg://deepresearch:deepresearch@localhost:5432/deepresearch",
            ),
            redis_url=os.getenv("REDIS_URL", "redis://localhost:6379/0"),
            celery_broker_url=os.getenv("CELERY_BROKER_URL", "redis://localhost:6379/1"),
            default_tenant_id=os.getenv("DEFAULT_TENANT_ID", "tenant_dev"),
            default_user_id=os.getenv("DEFAULT_USER_ID", "user_dev"),
            model_mode=os.getenv("DEEPRESEARCH_MODEL_MODE", "configured"),
            lease_seconds=int(os.getenv("TASK_LEASE_SECONDS", "300")),
            agent_max_steps=int(os.getenv("AGENT_MAX_STEPS", "6")),
            retrieval_branch_concurrency=int(
                os.getenv("RETRIEVAL_BRANCH_CONCURRENCY", "3")
            ),
            phase2_enabled=os.getenv("PHASE2_ENABLED", "1") not in {"0", "false", "False"},
            phase3_enabled=os.getenv("PHASE3_ENABLED", "1") not in {"0", "false", "False"},
            phase4_enabled=os.getenv("PHASE4_ENABLED", "1") not in {"0", "false", "False"},
            phase5_enabled=os.getenv("PHASE5_ENABLED", "1") not in {"0", "false", "False"},
            phase9_enabled=os.getenv("PHASE9_ENABLED", "1") not in {"0", "false", "False"},
            minio_endpoint=os.getenv("MINIO_ENDPOINT") or None,
            minio_access_key=os.getenv("MINIO_ACCESS_KEY", "minioadmin"),
            minio_secret_key=os.getenv("MINIO_SECRET_KEY", "minioadmin"),
            minio_secure=os.getenv("MINIO_SECURE", "0") in {"1", "true", "True"},
            elasticsearch_url=os.getenv("ELASTICSEARCH_URL") or None,
            elasticsearch_document_index=os.getenv(
                "ELASTICSEARCH_DOCUMENT_INDEX", "document_chunks_v1"
            ),
            elasticsearch_text_analyzer=os.getenv(
                "ELASTICSEARCH_TEXT_ANALYZER", "ik_max_word"
            ),
            elasticsearch_search_analyzer=os.getenv(
                "ELASTICSEARCH_SEARCH_ANALYZER", "ik_smart"
            ),
            tool_inline_budget=int(os.getenv("TOOL_INLINE_BUDGET", "32000")),
            sandbox_image=os.getenv(
                "SANDBOX_IMAGE", "python:3.12.7-slim-bookworm"
            ),
            sandbox_runtime=os.getenv("SANDBOX_RUNTIME", "runsc"),
            tenant_workspace_bytes_max=int(
                os.getenv("TENANT_WORKSPACE_BYTES_MAX", str(20 * 1024**3))
            ),
            task_workspace_bytes_max=int(
                os.getenv("TASK_WORKSPACE_BYTES_MAX", str(1 * 1024**3))
            ),
            workspace_file_max_bytes=int(
                os.getenv("WORKSPACE_FILE_MAX_BYTES", str(256 * 1024**2))
            ),
            query_completion_message_limit=int(
                os.getenv("QUERY_COMPLETION_MESSAGE_LIMIT", "12")
            ),
            query_completion_token_budget=int(
                os.getenv("QUERY_COMPLETION_TOKEN_BUDGET", "4000")
            ),
            ltm_candidate_k=int(os.getenv("LTM_CANDIDATE_K", "20")),
            ltm_top_k=int(os.getenv("LTM_TOP_K", "8")),
            ltm_min_similarity=float(os.getenv("LTM_MIN_SIMILARITY", "0.55")),
            ltm_min_lexical_score=float(os.getenv("LTM_MIN_LEXICAL_SCORE", "0.20")),
            ltm_token_budget=int(os.getenv("LTM_TOKEN_BUDGET", "1200")),
            ltm_write_confidence_threshold=float(
                os.getenv("LTM_WRITE_CONFIDENCE_THRESHOLD", "0.70")
            ),
            ltm_max_content_chars=int(os.getenv("LTM_MAX_CONTENT_CHARS", "500")),
            openai_chat_provider=os.getenv("OPENAI_CHAT_PROVIDER", "openai"),
            openai_base_url=os.getenv(
                "OPENAI_BASE_URL", "https://api.openai.com/v1"
            ),
            openai_chat_model=os.getenv("OPENAI_CHAT_MODEL", "gpt-4.1-mini"),
            openai_embedding_provider=os.getenv(
                "OPENAI_EMBEDDING_PROVIDER", "openai"
            ),
            openai_embedding_base_url=os.getenv(
                "OPENAI_EMBEDDING_BASE_URL", "https://api.openai.com/v1"
            ),
            openai_embedding_model=os.getenv(
                "OPENAI_EMBEDDING_MODEL", "text-embedding-3-small"
            ),
            openai_embedding_dimensions=int(
                os.getenv("OPENAI_EMBEDDING_DIMENSIONS", "1024")
            ),
            openai_secret_ref="OPENAI_API_KEY",
            openai_embedding_secret_ref="OPENAI_EMBEDDING_API_KEY",
            tavily_secret_ref="TAVILY_API_KEY",
            jina_base_url=os.getenv("JINA_BASE_URL", "https://api.jina.ai/v1"),
            jina_rerank_model=os.getenv(
                "JINA_RERANK_MODEL", "jina-reranker-v2-base-multilingual"
            ),
            jina_secret_ref="JINA_API_KEY",
            phase6_limits=Phase6Limits(
                readonly_sql_default_rows=int(
                    os.getenv("READONLY_SQL_DEFAULT_ROWS", "200")
                ),
                readonly_sql_max_rows=int(os.getenv("READONLY_SQL_MAX_ROWS", "1000")),
                readonly_sql_default_timeout_ms=int(
                    os.getenv("READONLY_SQL_DEFAULT_TIMEOUT_MS", "10000")
                ),
                readonly_sql_max_timeout_ms=int(
                    os.getenv("READONLY_SQL_MAX_TIMEOUT_MS", "30000")
                ),
                dataset_query_default_rows=int(
                    os.getenv("DATASET_QUERY_DEFAULT_ROWS", "100")
                ),
                dataset_query_max_rows=int(
                    os.getenv("DATASET_QUERY_MAX_ROWS", "1000")
                ),
                dataset_profile_default_columns=int(
                    os.getenv("DATASET_PROFILE_DEFAULT_COLUMNS", "50")
                ),
                dataset_profile_max_columns=int(
                    os.getenv("DATASET_PROFILE_MAX_COLUMNS", "100")
                ),
                data_upload_max_bytes=int(
                    os.getenv("DATA_UPLOAD_MAX_BYTES", str(256 * 1024**2))
                ),
                sandbox_cpus=float(os.getenv("SANDBOX_CPUS", "2")),
                sandbox_memory_bytes=int(
                    os.getenv("SANDBOX_MEMORY_BYTES", str(2 * 1024**3))
                ),
                sandbox_writable_disk_bytes=int(
                    os.getenv("SANDBOX_WRITABLE_DISK_BYTES", str(2 * 1024**3))
                ),
                sandbox_pids=int(os.getenv("SANDBOX_PIDS", "64")),
                sandbox_default_timeout_ms=int(
                    os.getenv("SANDBOX_DEFAULT_TIMEOUT_MS", "120000")
                ),
                sandbox_max_timeout_ms=int(
                    os.getenv("SANDBOX_MAX_TIMEOUT_MS", "300000")
                ),
                sandbox_log_bytes=int(
                    os.getenv("SANDBOX_LOG_BYTES", str(4 * 1024**2))
                ),
                sandbox_single_output_bytes=int(
                    os.getenv("SANDBOX_SINGLE_OUTPUT_BYTES", str(256 * 1024**2))
                ),
                sandbox_total_output_bytes=int(
                    os.getenv("SANDBOX_TOTAL_OUTPUT_BYTES", str(512 * 1024**2))
                ),
                chart_recommended_points=int(
                    os.getenv("CHART_RECOMMENDED_POINTS", "5000")
                ),
                chart_max_points=int(os.getenv("CHART_MAX_POINTS", "20000")),
                chart_max_series=int(os.getenv("CHART_MAX_SERIES", "20")),
                chart_max_categories=int(os.getenv("CHART_MAX_CATEGORIES", "200")),
                table_default_rows=int(os.getenv("TABLE_DEFAULT_ROWS", "100")),
                table_max_rows=int(os.getenv("TABLE_MAX_ROWS", "500")),
                table_max_columns=int(os.getenv("TABLE_MAX_COLUMNS", "30")),
                table_cell_max_bytes=int(os.getenv("TABLE_CELL_MAX_BYTES", "4096")),
                table_serialized_max_bytes=int(
                    os.getenv("TABLE_SERIALIZED_MAX_BYTES", str(256 * 1024))
                ),
            ),
            datasource_egress_allowlist=tuple(
                item.strip().lower()
                for item in os.getenv(
                    "DATASOURCE_EGRESS_ALLOWLIST", "localhost:5432"
                ).split(",")
                if item.strip()
            ),
            external_write_ledger_schema=os.getenv(
                "EXTERNAL_WRITE_LEDGER_SCHEMA", "public"
            ),
            external_write_ledger_table=os.getenv(
                "EXTERNAL_WRITE_LEDGER_TABLE", "agent_write_idempotency"
            ),
            neo4j_url=os.getenv("NEO4J_URL") or None,
            neo4j_username=os.getenv("NEO4J_USERNAME", "neo4j"),
            neo4j_password=os.getenv("NEO4J_PASSWORD", "deepresearch"),
            neo4j_database=os.getenv("NEO4J_DATABASE", "neo4j"),
            auth_enabled=os.getenv("PHASE10_AUTH_ENABLED", "1") not in {
                "0",
                "false",
                "False",
            },
            auth_jwt_algorithm=os.getenv("AUTH_JWT_ALGORITHM", "RS256"),
            auth_jwt_key_id=os.getenv("AUTH_JWT_KEY_ID", "phase10-dev"),
            auth_jwt_issuer=os.getenv(
                "AUTH_JWT_ISSUER", "deepresearch-mutilagent"
            ),
            auth_jwt_audience=os.getenv("AUTH_JWT_AUDIENCE", "deepresearch-api"),
            auth_jwt_private_key=(
                os.getenv("AUTH_JWT_PRIVATE_KEY", "").replace("\\n", "\n") or None
            ),
            auth_access_ttl_seconds=int(os.getenv("AUTH_ACCESS_TTL_SECONDS", "900")),
            auth_refresh_ttl_days=int(os.getenv("AUTH_REFRESH_TTL_DAYS", "30")),
            auth_refresh_cookie_name=os.getenv(
                "AUTH_REFRESH_COOKIE_NAME", "dr_refresh"
            ),
            auth_refresh_cookie_path=os.getenv(
                "AUTH_REFRESH_COOKIE_PATH", "/api/v1/auth"
            ),
            auth_cookie_secure=os.getenv("AUTH_COOKIE_SECURE", "1") not in {
                "0",
                "false",
                "False",
            },
            auth_cookie_samesite=os.getenv("AUTH_COOKIE_SAMESITE", "lax").lower(),
            auth_allowed_origins=tuple(
                item.strip()
                for item in os.getenv(
                    "AUTH_ALLOWED_ORIGINS",
                    "https://localhost,http://localhost:3000,http://localhost:5173",
                ).split(",")
                if item.strip()
            ),
            cursor_signing_key=os.getenv(
                "CURSOR_SIGNING_KEY", "phase10-development-cursor-key"
            ),
            cursor_ttl_seconds=int(os.getenv("CURSOR_TTL_SECONDS", "1800")),
            task_event_retention_days=int(
                os.getenv("TASK_EVENT_RETENTION_DAYS", "7")
            ),
            sse_heartbeat_seconds=int(os.getenv("SSE_HEARTBEAT_SECONDS", "15")),
            sse_connections_per_task_max=int(
                os.getenv("SSE_CONNECTIONS_PER_TASK_MAX", "5")
            ),
            sse_connections_per_user_max=int(
                os.getenv("SSE_CONNECTIONS_PER_USER_MAX", "20")
            ),
            sse_client_buffer_events_max=int(
                os.getenv("SSE_CLIENT_BUFFER_EVENTS_MAX", "256")
            ),
            sse_client_buffer_bytes_max=int(
                os.getenv("SSE_CLIENT_BUFFER_BYTES_MAX", "1048576")
            ),
            telemetry_enabled=os.getenv("TELEMETRY_ENABLED", "1") not in {
                "0",
                "false",
                "False",
            },
            telemetry_max_queue_size=int(
                os.getenv("TELEMETRY_MAX_QUEUE_SIZE", "2048")
            ),
            telemetry_max_export_batch_size=int(
                os.getenv("TELEMETRY_MAX_EXPORT_BATCH_SIZE", "256")
            ),
            telemetry_export_delay_millis=int(
                os.getenv("TELEMETRY_EXPORT_DELAY_MILLIS", "1000")
            ),
            trace_retention_days=int(os.getenv("TRACE_RETENTION_DAYS", "30")),
            audit_retention_days=int(os.getenv("AUDIT_RETENTION_DAYS", "365")),
            share_expires_default_days=int(
                os.getenv("SHARE_EXPIRES_DEFAULT_DAYS", "30")
            ),
            share_expires_max_days=int(os.getenv("SHARE_EXPIRES_MAX_DAYS", "90")),
            share_public_base_url=os.getenv("SHARE_PUBLIC_BASE_URL", ""),
            public_share_rate_limit_requests=int(
                os.getenv("PUBLIC_SHARE_RATE_LIMIT_REQUESTS", "60")
            ),
            public_share_rate_limit_window_seconds=int(
                os.getenv("PUBLIC_SHARE_RATE_LIMIT_WINDOW_SECONDS", "60")
            ),
            export_expires_days=int(os.getenv("EXPORT_EXPIRES_DAYS", "7")),
            export_max_attempts=int(os.getenv("EXPORT_MAX_ATTEMPTS", "3")),
            export_execution_timeout_seconds=int(
                os.getenv("EXPORT_EXECUTION_TIMEOUT_SECONDS", "900")
            ),
        )

    @staticmethod
    def _usable_secret(value: str | None) -> bool:
        return bool(
            value
            and value.strip()
            and value.strip().lower() not in PLACEHOLDER_SECRETS
        )

    def resolve_secret(self, secret_ref: str) -> str | None:
        value = os.getenv(secret_ref)
        return value if self._usable_secret(value) else None

    def missing_real_provider_secrets(self) -> tuple[str, ...]:
        if self.model_mode == "fake":
            return ()
        required = (
            self.openai_secret_ref,
            self.openai_embedding_secret_ref,
            self.tavily_secret_ref,
            self.jina_secret_ref,
        )
        return tuple(name for name in dict.fromkeys(required) if not self.resolve_secret(name))

    def require_real_provider_secrets(self) -> None:
        missing = self.missing_real_provider_secrets()
        if missing:
            raise MissingRealProviderSecretError(
                "BLOCKED_MISSING_REAL_SECRET: " + ", ".join(missing)
            )
