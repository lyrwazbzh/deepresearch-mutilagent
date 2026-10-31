BEGIN;

ALTER TABLE fact_memories
    ADD COLUMN IF NOT EXISTS storage_tier VARCHAR(16) NOT NULL DEFAULT 'hot',
    ADD COLUMN IF NOT EXISTS cold_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS rehydrated_at TIMESTAMPTZ;

ALTER TABLE experience_memories
    ADD COLUMN IF NOT EXISTS storage_tier VARCHAR(16) NOT NULL DEFAULT 'hot',
    ADD COLUMN IF NOT EXISTS cold_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS rehydrated_at TIMESTAMPTZ;

CREATE INDEX IF NOT EXISTS ix_fact_memory_user_tier
    ON fact_memories(user_id, storage_tier, status);
CREATE INDEX IF NOT EXISTS ix_experience_memory_user_tier
    ON experience_memories(user_id, storage_tier, status);

COMMIT;
