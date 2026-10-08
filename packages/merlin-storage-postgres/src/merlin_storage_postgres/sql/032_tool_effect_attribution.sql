ALTER TABLE merlin_storage.tool_effects
    ADD COLUMN agent_id text,
    ADD COLUMN attempt_id uuid REFERENCES merlin_storage.scheduler_attempts(id) ON DELETE RESTRICT,
    ADD COLUMN campaign_id text,
    ADD COLUMN target_id text;

ALTER TABLE merlin_storage.tool_effects
    DROP CONSTRAINT IF EXISTS tool_effects_session_id_call_id_key;

CREATE UNIQUE INDEX tool_effects_owned_call_idx
    ON merlin_storage.tool_effects
    (session_id, COALESCE(attempt_id::text, agent_id, 'legacy'), call_id, tool_name);

CREATE INDEX tool_effects_attempt_idx
    ON merlin_storage.tool_effects (attempt_id, updated_at DESC)
    WHERE attempt_id IS NOT NULL;
