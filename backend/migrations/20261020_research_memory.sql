BEGIN;

CREATE TABLE IF NOT EXISTS fact_memories (
    id UUID PRIMARY KEY,
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    canonical_key VARCHAR(64) NOT NULL,
    content_hash VARCHAR(64) NOT NULL,
    subject VARCHAR(500) NOT NULL,
    predicate VARCHAR(255) NOT NULL,
    value TEXT NOT NULL,
    content TEXT NOT NULL,
    source_url TEXT,
    source_name VARCHAR(500),
    source_type VARCHAR(80),
    confidence DOUBLE PRECISION NOT NULL DEFAULT 0.5,
    version INTEGER NOT NULL DEFAULT 1,
    parent_id UUID REFERENCES fact_memories(id) ON DELETE SET NULL,
    status VARCHAR(24) NOT NULL DEFAULT 'active',
    observed_at TIMESTAMPTZ,
    last_accessed_at TIMESTAMPTZ,
    access_count INTEGER NOT NULL DEFAULT 0,
    metadata_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_fact_memory_version UNIQUE (user_id, canonical_key, version)
);
CREATE INDEX IF NOT EXISTS ix_fact_memory_user_status ON fact_memories(user_id, status);
CREATE INDEX IF NOT EXISTS ix_fact_memory_canonical ON fact_memories(user_id, canonical_key);

CREATE TABLE IF NOT EXISTS experience_memories (
    id UUID PRIMARY KEY,
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    run_id VARCHAR(128) NOT NULL,
    scenario TEXT NOT NULL,
    trigger_pattern TEXT NOT NULL,
    strategy TEXT NOT NULL,
    outcome TEXT NOT NULL,
    critic_summary TEXT,
    initial_score DOUBLE PRECISION,
    final_score DOUBLE PRECISION,
    status VARCHAR(24) NOT NULL DEFAULT 'active',
    last_accessed_at TIMESTAMPTZ,
    access_count INTEGER NOT NULL DEFAULT 0,
    metadata_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS ix_experience_memory_user_status ON experience_memories(user_id, status);
CREATE INDEX IF NOT EXISTS ix_experience_memory_run ON experience_memories(run_id);

COMMIT;
