ALTER TABLE merlin_storage.browser_contexts
    DROP CONSTRAINT browser_contexts_mode_check;
ALTER TABLE merlin_storage.browser_contexts
    ADD CONSTRAINT browser_contexts_mode_check
    CHECK (mode IN ('isolated', 'attached', 'persistent'));

CREATE TABLE merlin_storage.campaign_lessons (
    id text PRIMARY KEY,
    campaign_id text NOT NULL
        REFERENCES merlin_storage.assessment_campaigns(id) ON DELETE CASCADE,
    session_id uuid NOT NULL REFERENCES merlin_storage.sessions(id) ON DELETE CASCADE,
    wave integer NOT NULL CHECK (wave >= 0),
    task_id text NOT NULL,
    profile_id text NOT NULL,
    target_id text NOT NULL,
    outcome text NOT NULL CHECK (outcome IN ('successful_setup', 'negative_result', 'finding')),
    content text NOT NULL,
    evidence_digest text NOT NULL CHECK (evidence_digest LIKE 'sha256:%'),
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (campaign_id, wave, task_id)
);
CREATE INDEX campaign_lessons_session_created_idx
    ON merlin_storage.campaign_lessons (session_id, created_at DESC);
