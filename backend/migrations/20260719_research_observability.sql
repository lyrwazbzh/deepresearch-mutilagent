BEGIN;

CREATE TABLE IF NOT EXISTS research_runs (
    id UUID PRIMARY KEY,
    research_id UUID NOT NULL,
    session_id UUID NOT NULL REFERENCES chat_sessions(id) ON DELETE CASCADE,
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    request_id VARCHAR(128),
    trace_id VARCHAR(64),
    status VARCHAR(24) NOT NULL DEFAULT 'running',
    current_phase VARCHAR(48),
    query_summary JSONB,
    metadata_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    ended_at TIMESTAMPTZ,
    duration_ms INTEGER,
    input_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    estimated_cost NUMERIC(14, 6),
    error_code VARCHAR(80),
    error_summary TEXT
);

CREATE INDEX IF NOT EXISTS ix_research_runs_research_id ON research_runs(research_id);
CREATE INDEX IF NOT EXISTS ix_research_runs_trace_id ON research_runs(trace_id);
CREATE INDEX IF NOT EXISTS ix_research_runs_status ON research_runs(status);
CREATE INDEX IF NOT EXISTS ix_research_runs_session_started ON research_runs(session_id, started_at);
CREATE INDEX IF NOT EXISTS ix_research_runs_research_started ON research_runs(research_id, started_at);
CREATE INDEX IF NOT EXISTS ix_research_runs_user_status ON research_runs(user_id, status);

CREATE TABLE IF NOT EXISTS research_events (
    id BIGSERIAL PRIMARY KEY,
    run_id UUID NOT NULL REFERENCES research_runs(id) ON DELETE CASCADE,
    sequence INTEGER NOT NULL,
    event_type VARCHAR(80) NOT NULL,
    phase VARCHAR(48),
    status VARCHAR(24) NOT NULL DEFAULT 'info',
    payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    duration_ms INTEGER,
    trace_id VARCHAR(64),
    span_id VARCHAR(32),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_research_events_run_sequence UNIQUE (run_id, sequence)
);

CREATE INDEX IF NOT EXISTS ix_research_events_run_created ON research_events(run_id, created_at);
CREATE INDEX IF NOT EXISTS ix_research_events_type_created ON research_events(event_type, created_at);

COMMIT;
