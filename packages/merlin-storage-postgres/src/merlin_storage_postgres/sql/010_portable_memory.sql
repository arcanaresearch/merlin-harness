CREATE TABLE merlin_storage.portable_memory_bundles (
    bundle_id text PRIMARY KEY,
    schema_version text NOT NULL,
    digest text NOT NULL UNIQUE,
    provenance jsonb NOT NULL,
    document jsonb NOT NULL,
    status text NOT NULL DEFAULT 'quarantined'
        CHECK (status IN ('quarantined', 'accepted', 'rejected', 'reviewed', 'deleted')),
    imported_at timestamptz NOT NULL DEFAULT now(),
    reviewed_at timestamptz,
    reviewed_by text,
    deleted_at timestamptz
);

CREATE TABLE merlin_storage.portable_memory_records (
    record_id text PRIMARY KEY,
    bundle_id text NOT NULL REFERENCES merlin_storage.portable_memory_bundles(bundle_id),
    kind text NOT NULL,
    digest text NOT NULL UNIQUE,
    payload jsonb NOT NULL,
    provenance jsonb NOT NULL,
    applicability jsonb NOT NULL,
    redactions jsonb NOT NULL,
    expires_at timestamptz,
    status text NOT NULL DEFAULT 'quarantined'
        CHECK (status IN ('quarantined', 'accepted', 'rejected', 'deleted')),
    reviewed_at timestamptz,
    reviewed_by text,
    rejection_reason text,
    deleted_at timestamptz
);

CREATE INDEX portable_memory_active_expiry_idx
    ON merlin_storage.portable_memory_records (status, expires_at)
    WHERE status = 'accepted';
CREATE INDEX portable_memory_applicability_idx
    ON merlin_storage.portable_memory_records USING gin (applicability);

CREATE TABLE merlin_storage.portable_memory_audit (
    id bigserial PRIMARY KEY,
    bundle_id text NOT NULL,
    record_id text,
    event text NOT NULL
        CHECK (event IN ('quarantined', 'accepted', 'rejected', 'deleted', 'used_in_turn')),
    operator_id text NOT NULL,
    reason text,
    provenance jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX portable_memory_audit_bundle_idx
    ON merlin_storage.portable_memory_audit (bundle_id, created_at);

CREATE TABLE merlin_storage.portable_memory_turn_uses (
    session_id text NOT NULL,
    turn_id text NOT NULL,
    record_id text NOT NULL REFERENCES merlin_storage.portable_memory_records(record_id),
    bundle_id text NOT NULL,
    operator_id text NOT NULL,
    applicability jsonb NOT NULL,
    recorded_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (session_id, turn_id, record_id)
);
