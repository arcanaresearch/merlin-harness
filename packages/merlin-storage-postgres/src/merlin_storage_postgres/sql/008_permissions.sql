CREATE TABLE merlin_storage.permission_rules (
    id uuid PRIMARY KEY,
    session_id uuid NOT NULL REFERENCES merlin_storage.sessions(id) ON DELETE CASCADE,
    tool_name text NOT NULL,
    effect text NOT NULL,
    scope_fingerprint text NOT NULL,
    decision text NOT NULL CHECK (decision IN ('allow_session', 'deny_session')),
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (session_id, tool_name, effect, scope_fingerprint)
);

CREATE TABLE merlin_storage.permission_audit (
    id uuid PRIMARY KEY,
    session_id uuid NOT NULL REFERENCES merlin_storage.sessions(id) ON DELETE CASCADE,
    tool_name text NOT NULL,
    effect text NOT NULL,
    scope_fingerprint text NOT NULL,
    workspace_root text NOT NULL,
    plugin_id text,
    target text,
    mode text NOT NULL,
    verdict text NOT NULL,
    decision text,
    reason text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX permission_audit_session_created_idx
    ON merlin_storage.permission_audit (session_id, created_at DESC);
