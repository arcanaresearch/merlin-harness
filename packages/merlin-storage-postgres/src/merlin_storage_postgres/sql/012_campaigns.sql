CREATE TABLE merlin_storage.assessment_campaigns (
    id text PRIMARY KEY,
    session_id uuid NOT NULL REFERENCES merlin_storage.sessions(id) ON DELETE CASCADE,
    manifest jsonb NOT NULL,
    manifest_digest text NOT NULL,
    status text NOT NULL CHECK (status IN ('prepared', 'executing', 'completed', 'cancelled', 'failed')),
    schedule_id uuid NULL REFERENCES merlin_storage.scheduler_schedules(id),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX assessment_campaigns_session_updated_idx
    ON merlin_storage.assessment_campaigns (session_id, updated_at DESC);
