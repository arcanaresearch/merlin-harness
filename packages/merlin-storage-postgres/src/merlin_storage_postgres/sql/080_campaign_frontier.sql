-- Campaign-owned progressive execution frontier. Large reviewed catalogues are
-- normalized into shared family documents plus compact candidate variants so
-- scheduler graphs only need to contain the currently claimed batch.
ALTER TABLE merlin_storage.campaign_waves
    DROP CONSTRAINT IF EXISTS campaign_waves_kind_check;

ALTER TABLE merlin_storage.campaign_waves
    ADD CONSTRAINT campaign_waves_kind_check
    CHECK (kind IN (
        'initial',
        'followup',
        'frontier',
        'validation',
        'reproduction',
        'review',
        'remediation',
        'operator-continuation'
    ));

COMMENT ON CONSTRAINT campaign_waves_kind_check ON merlin_storage.campaign_waves IS
    'Admits initial and progressive frontier attack batches, operator-requested continuation, validation, reproduction, lightweight review, and defensive remediation wave types.';

CREATE TABLE merlin_storage.campaign_frontier_families (
    campaign_id text NOT NULL
        REFERENCES merlin_storage.assessment_campaigns(id) ON DELETE CASCADE,
    id text NOT NULL CHECK (id <> ''),
    document jsonb NOT NULL CHECK (jsonb_typeof(document) = 'object'),
    document_digest text NOT NULL CHECK (document_digest ~ '^[0-9a-f]{64}$'),
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (campaign_id, id)
);

CREATE TABLE merlin_storage.campaign_frontier_batches (
    campaign_id text NOT NULL
        REFERENCES merlin_storage.assessment_campaigns(id) ON DELETE CASCADE,
    id text NOT NULL CHECK (id <> ''),
    wave integer NOT NULL CHECK (wave >= 0),
    state text NOT NULL CHECK (state IN ('claimed', 'scheduled', 'settled')),
    candidate_digest text NOT NULL CHECK (candidate_digest ~ '^[0-9a-f]{64}$'),
    schedule_id uuid REFERENCES merlin_storage.scheduler_schedules(id) ON DELETE RESTRICT,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (campaign_id, id),
    UNIQUE (campaign_id, wave),
    CHECK ((state = 'scheduled') = (schedule_id IS NOT NULL) OR state = 'settled')
);

CREATE UNIQUE INDEX campaign_frontier_one_active_batch_idx
    ON merlin_storage.campaign_frontier_batches (campaign_id)
    WHERE state IN ('claimed', 'scheduled');

CREATE TABLE merlin_storage.campaign_frontier_candidates (
    campaign_id text NOT NULL,
    id text NOT NULL CHECK (id <> ''),
    family_id text NOT NULL CHECK (family_id <> ''),
    ordinal bigint NOT NULL CHECK (ordinal >= 0),
    requested_language text NOT NULL CHECK (requested_language <> ''),
    variant_id text NOT NULL CHECK (variant_id <> ''),
    variant jsonb NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(variant) = 'object'),
    priority double precision,
    state text NOT NULL DEFAULT 'queued'
        CHECK (state IN ('dormant', 'queued', 'claimed', 'scheduled', 'terminal')),
    claim_generation integer NOT NULL DEFAULT 0 CHECK (claim_generation >= 0),
    active_batch_id text,
    terminal_reason text,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (campaign_id, id),
    FOREIGN KEY (campaign_id, family_id)
        REFERENCES merlin_storage.campaign_frontier_families(campaign_id, id)
        ON DELETE RESTRICT,
    FOREIGN KEY (campaign_id, active_batch_id)
        REFERENCES merlin_storage.campaign_frontier_batches(campaign_id, id)
        ON DELETE RESTRICT,
    UNIQUE (campaign_id, ordinal),
    UNIQUE (campaign_id, family_id, requested_language, variant_id),
    CHECK ((state IN ('claimed', 'scheduled')) = (active_batch_id IS NOT NULL)),
    CHECK ((state = 'terminal') = (terminal_reason IS NOT NULL))
);

CREATE INDEX campaign_frontier_candidate_queue_idx
    ON merlin_storage.campaign_frontier_candidates
    (campaign_id, state, priority DESC NULLS LAST, ordinal, id);

CREATE TABLE merlin_storage.campaign_frontier_batch_items (
    campaign_id text NOT NULL,
    batch_id text NOT NULL,
    candidate_id text NOT NULL,
    position integer NOT NULL CHECK (position >= 0),
    disposition text CHECK (disposition IN ('terminal', 'retryable', 'requeue')),
    reason text,
    settled_at timestamptz,
    PRIMARY KEY (campaign_id, batch_id, candidate_id),
    UNIQUE (campaign_id, batch_id, position),
    FOREIGN KEY (campaign_id, batch_id)
        REFERENCES merlin_storage.campaign_frontier_batches(campaign_id, id)
        ON DELETE CASCADE,
    FOREIGN KEY (campaign_id, candidate_id)
        REFERENCES merlin_storage.campaign_frontier_candidates(campaign_id, id)
        ON DELETE RESTRICT,
    CHECK ((disposition IS NULL) = (settled_at IS NULL)),
    CHECK (disposition <> 'terminal' OR reason IS NOT NULL)
);

COMMENT ON TABLE merlin_storage.campaign_frontier_families IS
    'Deduplicated immutable documents shared by campaign frontier candidates.';
COMMENT ON TABLE merlin_storage.campaign_frontier_candidates IS
    'Campaign-specific logical attack frontier; queued and dormant work remains durable across scheduler batches.';
COMMENT ON TABLE merlin_storage.campaign_frontier_batches IS
    'Idempotent bounded claims that bind one deterministic candidate set to at most one scheduler wave.';
