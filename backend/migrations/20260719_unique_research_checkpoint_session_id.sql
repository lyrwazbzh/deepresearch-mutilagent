-- Abort without changing data when duplicate session IDs exist.
SELECT session_id, COUNT(*) AS duplicate_count
FROM research_checkpoints
GROUP BY session_id
HAVING COUNT(*) > 1;

DO $$
BEGIN
    IF EXISTS (
        SELECT 1
        FROM research_checkpoints
        GROUP BY session_id
        HAVING COUNT(*) > 1
    ) THEN
        RAISE EXCEPTION
            'Duplicate research checkpoint session_id values exist; resolve them before adding the unique index';
    END IF;
END
$$;

CREATE UNIQUE INDEX IF NOT EXISTS uq_research_checkpoints_session_id
ON research_checkpoints (session_id);
