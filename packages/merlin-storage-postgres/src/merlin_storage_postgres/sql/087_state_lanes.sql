-- One row is the authoritative target-state lane for one logical experiment.
-- Individual browser/API/recon actions bind the lane revision they observed at
-- admission.  Settlement advances that revision with CAS semantics, preventing
-- stale work from being attributed to a newer target state.
CREATE TABLE merlin_storage.state_lanes (
    id text PRIMARY KEY CHECK (id <> ''),
    campaign_id text NOT NULL
        REFERENCES merlin_storage.assessment_campaigns(id) ON DELETE CASCADE,
    experiment_id text NOT NULL CHECK (experiment_id <> ''),
    target_authority text NOT NULL CHECK (target_authority <> ''),
    principal_key text,
    physical_state_key text,
    physical_lane_id text,
    physical_lane_revision integer CHECK (physical_lane_revision >= 0),
    conservative_lane_revision integer NOT NULL DEFAULT 0
        CHECK (conservative_lane_revision >= 0),
    conservative_ambiguity_revision integer NOT NULL DEFAULT 0
        CHECK (conservative_ambiguity_revision >= 0),
    revision integer NOT NULL DEFAULT 1 CHECK (revision > 0),
    status text NOT NULL DEFAULT 'current'
        CHECK (status IN ('current', 'reobservation_required')),
    last_transition text NOT NULL DEFAULT 'none'
        CHECK (last_transition IN ('none', 'attributed', 'unattributed')),
    latest_observation_digest text
        CHECK (
            latest_observation_digest IS NULL
            OR latest_observation_digest ~ '^sha256:[0-9a-f]{64}$'
        ),
    latest_scope_id text,
    reobserved_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (campaign_id, experiment_id, target_authority),
    CHECK (principal_key IS NULL OR principal_key <> ''),
    CHECK (physical_state_key IS NULL OR physical_state_key <> '')
);

-- One row is the shared revision clock for one secret-safe physical target
-- state. Experiment lanes retain their last observed revision of this clock,
-- so A1 -> B1 -> A2 cannot silently attribute B's mutation to A.
CREATE TABLE merlin_storage.state_physical_lanes (
    id text PRIMARY KEY CHECK (id <> ''),
    target_authority text NOT NULL CHECK (target_authority <> ''),
    principal_key text NOT NULL CHECK (principal_key <> ''),
    physical_state_key text NOT NULL CHECK (physical_state_key <> ''),
    revision integer NOT NULL DEFAULT 0 CHECK (revision >= 0),
    ambiguity_revision integer NOT NULL DEFAULT 0
        CHECK (ambiguity_revision >= 0),
    latest_experiment_id text,
    latest_scope_id text,
    latest_observation_digest text
        CHECK (
            latest_observation_digest IS NULL
            OR latest_observation_digest ~ '^sha256:[0-9a-f]{64}$'
        ),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (target_authority, principal_key, physical_state_key)
);

ALTER TABLE merlin_storage.state_lanes
    ADD CONSTRAINT state_lanes_physical_lane_fk
    FOREIGN KEY (physical_lane_id)
    REFERENCES merlin_storage.state_physical_lanes(id) ON DELETE RESTRICT;

CREATE TABLE merlin_storage.state_lane_revisions (
    lane_id text NOT NULL
        REFERENCES merlin_storage.state_lanes(id) ON DELETE CASCADE,
    revision integer NOT NULL CHECK (revision > 0),
    scope_id text,
    transition text NOT NULL
        CHECK (transition IN ('created', 'attributed', 'unattributed')),
    principal_key text,
    physical_state_key text,
    observation_digest text
        CHECK (
            observation_digest IS NULL
            OR observation_digest ~ '^sha256:[0-9a-f]{64}$'
        ),
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (lane_id, revision)
);

ALTER TABLE merlin_storage.state_scopes
    ADD COLUMN lane_id text
        REFERENCES merlin_storage.state_lanes(id) ON DELETE RESTRICT,
    ADD COLUMN expected_lane_revision integer CHECK (expected_lane_revision > 0),
    ADD COLUMN expected_physical_lane_id text
        REFERENCES merlin_storage.state_physical_lanes(id) ON DELETE RESTRICT,
    ADD COLUMN expected_physical_lane_revision integer
        CHECK (expected_physical_lane_revision >= 0),
    ADD COLUMN expected_conservative_lane_revision integer NOT NULL DEFAULT 0
        CHECK (expected_conservative_lane_revision >= 0),
    ADD COLUMN expected_conservative_ambiguity_revision integer NOT NULL DEFAULT 0
        CHECK (expected_conservative_ambiguity_revision >= 0);

CREATE INDEX state_lane_reobservation_idx
    ON merlin_storage.state_lanes (campaign_id, status, updated_at);
