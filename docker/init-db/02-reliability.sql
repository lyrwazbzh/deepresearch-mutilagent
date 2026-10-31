BEGIN;

CREATE TABLE IF NOT EXISTS tool_executions (
    id UUID PRIMARY KEY,
    user_id UUID REFERENCES users(id) ON DELETE CASCADE,
    run_id VARCHAR(128) NOT NULL,
    session_id VARCHAR(128) NOT NULL,
    idempotency_key VARCHAR(64) NOT NULL,
    operation VARCHAR(160) NOT NULL,
    provider VARCHAR(120) NOT NULL,
    request_hash VARCHAR(64),
    status VARCHAR(24) NOT NULL DEFAULT 'running',
    attempts INTEGER NOT NULL DEFAULT 1,
    result_json JSONB,
    error_json JSONB,
    lease_expires_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_tool_executions_idempotency_key UNIQUE (idempotency_key)
);
CREATE INDEX IF NOT EXISTS ix_tool_executions_run_status ON tool_executions(run_id, status);

CREATE TABLE IF NOT EXISTS dead_letter_tasks (
    id UUID PRIMARY KEY,
    user_id UUID REFERENCES users(id) ON DELETE CASCADE,
    run_id VARCHAR(128) NOT NULL,
    session_id VARCHAR(128) NOT NULL,
    operation VARCHAR(160) NOT NULL,
    provider VARCHAR(120) NOT NULL,
    sanitized_payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    error_code VARCHAR(120) NOT NULL,
    error_summary TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    status VARCHAR(24) NOT NULL DEFAULT 'pending',
    available_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS ix_dead_letter_user_status ON dead_letter_tasks(user_id, status);
CREATE INDEX IF NOT EXISTS ix_dead_letter_available ON dead_letter_tasks(status, available_at);

CREATE TABLE IF NOT EXISTS search_index_outbox (
    id UUID PRIMARY KEY,
    operation VARCHAR(24) NOT NULL,
    payload_json JSONB NOT NULL,
    status VARCHAR(24) NOT NULL DEFAULT 'pending',
    attempts INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    available_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS ix_search_index_outbox_status ON search_index_outbox(status, available_at);

COMMIT;
