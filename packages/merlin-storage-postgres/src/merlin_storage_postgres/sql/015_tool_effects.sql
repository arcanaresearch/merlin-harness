ALTER TABLE merlin_storage.permission_rules
    ADD COLUMN expires_at timestamptz;

CREATE TABLE merlin_storage.tool_effects (
    idempotency_key text PRIMARY KEY,
    session_id uuid NOT NULL REFERENCES merlin_storage.sessions(id) ON DELETE CASCADE,
    call_id text NOT NULL,
    tool_name text NOT NULL,
    effect text NOT NULL,
    arguments_digest text NOT NULL,
    state text NOT NULL CHECK (state IN ('intent', 'acknowledged', 'ambiguous', 'reconciled')),
    result_content text,
    result_is_error boolean,
    result_metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
    reconciliation text,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (session_id, call_id)
);

CREATE INDEX tool_effects_session_updated_idx
    ON merlin_storage.tool_effects (session_id, updated_at DESC);

CREATE TABLE merlin_storage.cleanup_receipts (
    idempotency_key text PRIMARY KEY,
    session_id uuid NOT NULL REFERENCES merlin_storage.sessions(id) ON DELETE CASCADE,
    resource_kind text NOT NULL,
    resource_id text NOT NULL,
    state text NOT NULL,
    details jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (session_id, resource_kind, resource_id, state)
);

CREATE INDEX cleanup_receipts_session_created_idx
    ON merlin_storage.cleanup_receipts (session_id, created_at DESC);
