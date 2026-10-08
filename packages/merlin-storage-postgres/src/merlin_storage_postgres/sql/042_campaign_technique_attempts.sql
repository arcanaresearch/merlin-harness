CREATE TABLE merlin_storage.campaign_technique_attempts (
    id uuid PRIMARY KEY,
    schema_version smallint NOT NULL DEFAULT 1 CHECK (schema_version = 1),
    campaign_id text NOT NULL CHECK (campaign_id <> ''),
    wave integer NOT NULL CHECK (wave >= 0),
    task_id text NOT NULL CHECK (task_id <> ''),
    technique_id text NOT NULL CHECK (technique_id <> ''),
    strategy_id text NOT NULL CHECK (strategy_id <> ''),
    hypothesis_id text,
    parent_attempt_id uuid REFERENCES merlin_storage.campaign_technique_attempts(id) ON DELETE RESTRICT,
    transform_id text,
    language text NOT NULL DEFAULT 'und' CHECK (language <> ''),
    control_kind text NOT NULL CHECK (
        control_kind IN ('attack', 'negative_control', 'positive_control', 'validation')
    ),
    oracle_digest text NOT NULL CHECK (oracle_digest ~ '^sha256:[0-9a-f]{64}$'),
    prompt_digest text CHECK (prompt_digest IS NULL OR prompt_digest ~ '^sha256:[0-9a-f]{64}$'),
    scheduler_attempt_id uuid REFERENCES merlin_storage.scheduler_attempts(id) ON DELETE RESTRICT,
    state text NOT NULL CHECK (
        state IN ('planned', 'active', 'effect_acknowledged', 'observed', 'confirmed',
                  'refuted', 'inconclusive', 'blocked', 'cancelled', 'ambiguous')
    ),
    strategy_state jsonb NOT NULL DEFAULT '{}',
    evidence_refs text[] NOT NULL DEFAULT '{}',
    provider text,
    model text,
    usage jsonb NOT NULL DEFAULT '{}',
    terminal_reason text,
    created_at timestamptz NOT NULL DEFAULT now(),
    activated_at timestamptz,
    updated_at timestamptz NOT NULL DEFAULT now(),
    terminal_at timestamptz,
    CHECK (parent_attempt_id IS NULL OR parent_attempt_id <> id),
    CHECK ((state = 'planned') = (activated_at IS NULL)),
    CHECK (
        (state IN ('confirmed', 'refuted', 'inconclusive', 'blocked', 'cancelled', 'ambiguous'))
        = (terminal_at IS NOT NULL AND coalesce(terminal_reason, '') <> '')
    ),
    UNIQUE (campaign_id, wave, task_id, technique_id, strategy_id, control_kind, language,
            transform_id, hypothesis_id)
);

CREATE UNIQUE INDEX campaign_technique_attempt_active_scheduler_idx
    ON merlin_storage.campaign_technique_attempts (scheduler_attempt_id)
    WHERE scheduler_attempt_id IS NOT NULL;

CREATE INDEX campaign_technique_attempt_frontier_idx
    ON merlin_storage.campaign_technique_attempts (campaign_id, state, wave, task_id);

CREATE INDEX campaign_technique_attempt_comparable_idx
    ON merlin_storage.campaign_technique_attempts
       (technique_id, strategy_id, oracle_digest, control_kind, language, created_at DESC);

COMMENT ON TABLE merlin_storage.campaign_technique_attempts IS
    'Authoritative lifecycle and lineage join for admitted target experiments; evidence payloads remain in their native stores.';
COMMENT ON COLUMN merlin_storage.campaign_technique_attempts.id IS 'Stable technique-attempt identity bound before the first target effect.';
COMMENT ON COLUMN merlin_storage.campaign_technique_attempts.schema_version IS 'Version of the typed TechniqueAttempt contract.';
COMMENT ON COLUMN merlin_storage.campaign_technique_attempts.campaign_id IS 'Owning assessment campaign identity.';
COMMENT ON COLUMN merlin_storage.campaign_technique_attempts.wave IS 'Zero-based campaign wave in which the experiment was admitted.';
COMMENT ON COLUMN merlin_storage.campaign_technique_attempts.task_id IS 'Owning admitted worker task identity.';
COMMENT ON COLUMN merlin_storage.campaign_technique_attempts.technique_id IS 'Concrete admitted technique or coverage-cell identity.';
COMMENT ON COLUMN merlin_storage.campaign_technique_attempts.strategy_id IS 'Execution strategy selected for this experiment.';
COMMENT ON COLUMN merlin_storage.campaign_technique_attempts.hypothesis_id IS 'Optional target-derived hypothesis identity.';
COMMENT ON COLUMN merlin_storage.campaign_technique_attempts.parent_attempt_id IS 'Optional predecessor experiment that caused this follow-up.';
COMMENT ON COLUMN merlin_storage.campaign_technique_attempts.transform_id IS 'Optional prompt or procedure transform identity.';
COMMENT ON COLUMN merlin_storage.campaign_technique_attempts.language IS 'BCP-47 language tag, or und when language is not specified.';
COMMENT ON COLUMN merlin_storage.campaign_technique_attempts.control_kind IS 'Attack, control, or independent-validation role of the experiment.';
COMMENT ON COLUMN merlin_storage.campaign_technique_attempts.oracle_digest IS 'Digest of the sealed success/refutation oracle contract.';
COMMENT ON COLUMN merlin_storage.campaign_technique_attempts.prompt_digest IS 'Digest of the concrete prompt after it has been selected.';
COMMENT ON COLUMN merlin_storage.campaign_technique_attempts.scheduler_attempt_id IS 'Optional active scheduler lease implementing this experiment.';
COMMENT ON COLUMN merlin_storage.campaign_technique_attempts.state IS 'Current monotonic lifecycle state.';
COMMENT ON COLUMN merlin_storage.campaign_technique_attempts.strategy_state IS 'Versioned strategy checkpoint data, not evidence payloads.';
COMMENT ON COLUMN merlin_storage.campaign_technique_attempts.evidence_refs IS 'Content-addressed references to evidence stored elsewhere.';
COMMENT ON COLUMN merlin_storage.campaign_technique_attempts.provider IS 'Provider route used for the specialist inference.';
COMMENT ON COLUMN merlin_storage.campaign_technique_attempts.model IS 'Model used for the specialist inference.';
COMMENT ON COLUMN merlin_storage.campaign_technique_attempts.usage IS 'Provider-reported and locally estimated usage metadata.';
COMMENT ON COLUMN merlin_storage.campaign_technique_attempts.terminal_reason IS 'Required human-readable reason for a terminal disposition.';
COMMENT ON COLUMN merlin_storage.campaign_technique_attempts.created_at IS 'Time the experiment was durably planned.';
COMMENT ON COLUMN merlin_storage.campaign_technique_attempts.activated_at IS 'Time execution first became active.';
COMMENT ON COLUMN merlin_storage.campaign_technique_attempts.updated_at IS 'Time of the latest accepted state transition.';
COMMENT ON COLUMN merlin_storage.campaign_technique_attempts.terminal_at IS 'Time the experiment reached an immutable terminal disposition.';
