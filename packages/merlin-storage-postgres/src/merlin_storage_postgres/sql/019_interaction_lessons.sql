CREATE TABLE merlin_storage.interaction_lessons (
    id text PRIMARY KEY,
    target_id text NOT NULL,
    interface_key text NOT NULL,
    document jsonb NOT NULL,
    digest text NOT NULL UNIQUE CHECK (digest LIKE 'sha256:%'),
    verified_by text NOT NULL,
    verified_at timestamptz NOT NULL,
    source_campaign_id text NOT NULL,
    source_task_id text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX interaction_lessons_target_verified_idx
    ON merlin_storage.interaction_lessons
    (target_id, interface_key, verified_at DESC, id);
