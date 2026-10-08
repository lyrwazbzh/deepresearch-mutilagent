CREATE TABLE IF NOT EXISTS public.agent_write_idempotency (
    operation_key varchar(64) PRIMARY KEY,
    write_id varchar(64) NOT NULL,
    plan_hash varchar(64) NOT NULL,
    status varchar(16) NOT NULL,
    affected_rows bigint NOT NULL DEFAULT 0,
    result_hash varchar(64),
    result_json jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    committed_at timestamptz,
    CONSTRAINT ck_agent_write_status CHECK (status IN ('executing', 'executed')),
    CONSTRAINT ck_agent_write_affected_rows CHECK (affected_rows >= 0),
    CONSTRAINT ck_agent_write_operation_key CHECK (length(operation_key) = 64),
    CONSTRAINT ck_agent_write_plan_hash CHECK (length(plan_hash) = 64)
);

CREATE INDEX IF NOT EXISTS ix_agent_write_idempotency_write
    ON public.agent_write_idempotency (write_id, created_at);
