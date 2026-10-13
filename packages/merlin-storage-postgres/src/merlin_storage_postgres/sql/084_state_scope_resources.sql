-- Universal, versioned state-transition envelopes. Existing campaigns remain
-- legacy-readable; creating the first scope upgrades only that campaign's
-- execution format without rewriting its reviewed manifest or evidence.
CREATE TABLE merlin_storage.scope_formats (
    version text PRIMARY KEY CHECK (version <> ''),
    claims_enabled boolean NOT NULL DEFAULT true,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

INSERT INTO merlin_storage.scope_formats (version, claims_enabled)
VALUES ('merlin.state-scope.v1', true);

ALTER TABLE merlin_storage.assessment_campaigns
    ADD COLUMN execution_format text NOT NULL DEFAULT 'merlin.technique-attempt.v1'
        CHECK (execution_format <> ''),
    ADD COLUMN lifecycle_pause_reason text
        CHECK (lifecycle_pause_reason IN (
            'fixed_forward', 'quota', 'acknowledged_settlement', 'cleanup'
        )),
    ADD COLUMN lifecycle_resume_status text
        CHECK (lifecycle_resume_status IN ('prepared', 'executing', 'paused')),
    ADD COLUMN lifecycle_wake_at timestamptz,
    ADD CONSTRAINT assessment_campaigns_lifecycle_pause_check CHECK (
        (lifecycle_pause_reason IS NULL OR status = 'paused')
        AND ((lifecycle_pause_reason IS NULL) = (lifecycle_resume_status IS NULL))
        AND (lifecycle_pause_reason = 'quota' OR lifecycle_wake_at IS NULL)
    );

CREATE TABLE merlin_storage.state_scopes (
    id text PRIMARY KEY CHECK (id <> ''),
    format_version text NOT NULL
        REFERENCES merlin_storage.scope_formats(version) ON DELETE RESTRICT,
    campaign_id text NOT NULL
        REFERENCES merlin_storage.assessment_campaigns(id) ON DELETE CASCADE,
    experiment_id text NOT NULL CHECK (experiment_id <> ''),
    attempt_id text NOT NULL CHECK (attempt_id <> ''),
    candidate_id text,
    action_id text NOT NULL CHECK (action_id <> ''),
    action_kind text NOT NULL CHECK (action_kind IN (
        'reconnaissance', 'baseline', 'candidate', 'validation', 'cleanup'
    )),
    target_authority text NOT NULL CHECK (target_authority <> ''),
    principal_key text,
    physical_state_key text,
    isolation_disposition text NOT NULL DEFAULT 'unknown'
        CHECK (isolation_disposition IN (
            'fresh', 'restored', 'cloned', 'shared_serialized', 'contaminated', 'unknown'
        )),
    transition_disposition text
        CHECK (transition_disposition IN (
            'progress', 'regression', 'terminal_success', 'terminal_failure',
            'unchanged', 'ambiguous', 'infrastructure_failure'
        )),
    cleanup_disposition text
        CHECK (cleanup_disposition IN (
            'not_required', 'pending', 'completed', 'failed', 'retained'
        )),
    status text NOT NULL DEFAULT 'open'
        CHECK (status IN ('open', 'effect_acknowledged', 'settled', 'paused')),
    current_revision integer NOT NULL DEFAULT 1 CHECK (current_revision > 0),
    created_at timestamptz NOT NULL DEFAULT now(),
    effect_acknowledged_at timestamptz,
    settled_at timestamptz,
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (campaign_id, action_id),
    CHECK (candidate_id IS NULL OR candidate_id <> ''),
    CHECK (principal_key IS NULL OR principal_key <> ''),
    CHECK (physical_state_key IS NULL OR physical_state_key <> ''),
    CHECK ((status IN ('effect_acknowledged', 'settled')) =
           (effect_acknowledged_at IS NOT NULL)),
    CHECK ((status = 'settled') = (settled_at IS NOT NULL)),
    CHECK ((status IN ('settled', 'paused')) =
           (transition_disposition IS NOT NULL AND cleanup_disposition IS NOT NULL))
);

CREATE INDEX state_scope_recovery_idx
    ON merlin_storage.state_scopes (campaign_id, status, updated_at);

CREATE TABLE merlin_storage.scope_envelopes (
    scope_id text NOT NULL
        REFERENCES merlin_storage.state_scopes(id) ON DELETE CASCADE,
    revision integer NOT NULL CHECK (revision > 0),
    envelope jsonb NOT NULL CHECK (jsonb_typeof(envelope) = 'object'),
    envelope_digest text NOT NULL
        CHECK (envelope_digest ~ '^sha256:[0-9a-f]{64}$'),
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (scope_id, revision),
    UNIQUE (scope_id, envelope_digest)
);

-- One global lease protects one reviewed physical-state equivalence class.
-- Campaign identity is deliberately absent from the ownership key.
CREATE TABLE merlin_storage.state_scope_leases (
    target_authority text NOT NULL CHECK (target_authority <> ''),
    principal_key text NOT NULL CHECK (principal_key <> ''),
    equivalence_key text NOT NULL CHECK (equivalence_key <> ''),
    claim_id uuid NOT NULL UNIQUE,
    scope_id text NOT NULL UNIQUE
        REFERENCES merlin_storage.state_scopes(id) ON DELETE CASCADE,
    claimant_id text NOT NULL CHECK (claimant_id <> ''),
    expires_at timestamptz NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (target_authority, principal_key, equivalence_key)
);

CREATE INDEX state_scope_lease_expiry_idx
    ON merlin_storage.state_scope_leases (expires_at);

-- One immutable checkpoint handle per reviewed prerequisite gate. The handle
-- is an internal opaque reference, never a raw target credential or token.
CREATE TABLE merlin_storage.gate_checkpoints (
    campaign_id text NOT NULL
        REFERENCES merlin_storage.assessment_campaigns(id) ON DELETE CASCADE,
    gate_id text NOT NULL CHECK (gate_id <> ''),
    manifest_digest text NOT NULL CHECK (manifest_digest <> ''),
    recipe_digest text NOT NULL CHECK (recipe_digest ~ '^sha256:[0-9a-f]{64}$'),
    checkpoint_handle jsonb NOT NULL CHECK (
        jsonb_typeof(checkpoint_handle) = 'object'
        AND checkpoint_handle ->> 'kind' = 'opaque_reference'
        AND checkpoint_handle ->> 'namespace' = 'state'
    ),
    evidence_digest text NOT NULL CHECK (evidence_digest ~ '^sha256:[0-9a-f]{64}$'),
    captured_scope_id text NOT NULL
        REFERENCES merlin_storage.state_scopes(id) ON DELETE RESTRICT,
    capture_claim_id uuid NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (campaign_id, gate_id)
);

-- Restore attempts are cardinality-many: the same reviewed checkpoint may be
-- restored before several independent experiments.
CREATE TABLE merlin_storage.checkpoint_restores (
    campaign_id text NOT NULL,
    gate_id text NOT NULL,
    scope_id text NOT NULL
        REFERENCES merlin_storage.state_scopes(id) ON DELETE RESTRICT,
    claim_id uuid NOT NULL,
    claimant_id text NOT NULL CHECK (claimant_id <> ''),
    state text NOT NULL DEFAULT 'claimed'
        CHECK (state IN ('claimed', 'restored', 'failed')),
    evidence_digest text CHECK (
        evidence_digest IS NULL OR evidence_digest ~ '^sha256:[0-9a-f]{64}$'
    ),
    created_at timestamptz NOT NULL DEFAULT now(),
    restored_at timestamptz,
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (campaign_id, gate_id, scope_id),
    FOREIGN KEY (campaign_id, gate_id)
        REFERENCES merlin_storage.gate_checkpoints(campaign_id, gate_id)
        ON DELETE CASCADE,
    CHECK ((state = 'restored') = (restored_at IS NOT NULL)),
    CHECK ((state = 'restored') = (evidence_digest IS NOT NULL))
);

-- One row is one principal-owned target resource window shared by every
-- campaign using the same target authority and safe principal identity.
CREATE TABLE merlin_storage.principal_resources (
    target_authority text NOT NULL CHECK (target_authority <> ''),
    principal_key text NOT NULL CHECK (principal_key <> ''),
    resource_kind text NOT NULL CHECK (resource_kind <> ''),
    window_key text NOT NULL CHECK (window_key <> ''),
    capacity bigint NOT NULL CHECK (capacity > 0),
    reserved bigint NOT NULL DEFAULT 0 CHECK (reserved >= 0),
    consumed bigint NOT NULL DEFAULT 0 CHECK (consumed >= 0),
    state text NOT NULL DEFAULT 'active'
        CHECK (state IN ('active', 'suspended', 'exhausted')),
    generation integer NOT NULL DEFAULT 1 CHECK (generation > 0),
    wake_at timestamptz,
    evidence jsonb NOT NULL DEFAULT '{}'::jsonb
        CHECK (jsonb_typeof(evidence) = 'object'),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (target_authority, principal_key, resource_kind, window_key),
    CHECK (state <> 'active' OR wake_at IS NULL)
);

CREATE INDEX principal_resource_wake_idx
    ON merlin_storage.principal_resources (wake_at)
    WHERE state = 'suspended';

CREATE TABLE merlin_storage.resource_reservations (
    id text PRIMARY KEY CHECK (id <> ''),
    scope_id text NOT NULL
        REFERENCES merlin_storage.state_scopes(id) ON DELETE RESTRICT,
    target_authority text NOT NULL,
    principal_key text NOT NULL,
    resource_kind text NOT NULL,
    window_key text NOT NULL,
    amount bigint NOT NULL CHECK (amount > 0),
    actual_amount bigint CHECK (actual_amount IS NULL OR actual_amount >= 0),
    state text NOT NULL DEFAULT 'reserved'
        CHECK (state IN ('reserved', 'acknowledged', 'settled', 'released')),
    created_at timestamptz NOT NULL DEFAULT now(),
    acknowledged_at timestamptz,
    settled_at timestamptz,
    updated_at timestamptz NOT NULL DEFAULT now(),
    FOREIGN KEY (target_authority, principal_key, resource_kind, window_key)
        REFERENCES merlin_storage.principal_resources(
            target_authority, principal_key, resource_kind, window_key
        ) ON DELETE RESTRICT,
    UNIQUE (scope_id, resource_kind, window_key),
    CHECK ((state IN ('acknowledged', 'settled')) = (acknowledged_at IS NOT NULL)),
    CHECK ((state IN ('settled', 'released')) = (settled_at IS NOT NULL)),
    CHECK ((state = 'settled') = (actual_amount IS NOT NULL))
);

CREATE INDEX resource_reservation_active_idx
    ON merlin_storage.resource_reservations
    (target_authority, principal_key, resource_kind, window_key, state)
    WHERE state IN ('reserved', 'acknowledged');

COMMENT ON TABLE merlin_storage.state_scopes IS
    'One durable logical target action and its state-transition lifecycle.';
COMMENT ON TABLE merlin_storage.scope_envelopes IS
    'Immutable versioned projections of a state scope; prior revisions are never rewritten.';
COMMENT ON TABLE merlin_storage.state_scope_leases IS
    'Global physical-state equivalence leases spanning campaigns and daemon processes.';
COMMENT ON TABLE merlin_storage.gate_checkpoints IS
    'One secret-safe durable checkpoint handle bound to a reviewed prerequisite recipe.';
COMMENT ON TABLE merlin_storage.checkpoint_restores IS
    'Fenced restore attempts for repeated independent experiments using one checkpoint.';
COMMENT ON TABLE merlin_storage.principal_resources IS
    'Global principal-owned quota and rate-limit windows shared across campaigns.';
COMMENT ON TABLE merlin_storage.resource_reservations IS
    'Idempotent target-resource reservations bound to one state scope.';
