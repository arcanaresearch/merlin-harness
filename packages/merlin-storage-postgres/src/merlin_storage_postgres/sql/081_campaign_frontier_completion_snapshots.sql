-- Immutable, normalized frontier completion snapshots keep authoritative
-- candidate outcomes out of campaign status and journal JSONB documents.
CREATE TABLE merlin_storage.campaign_frontier_completion_snapshots (
    id text PRIMARY KEY CHECK (id <> ''),
    sequence bigint GENERATED ALWAYS AS IDENTITY UNIQUE,
    campaign_id text NOT NULL
        REFERENCES merlin_storage.assessment_campaigns(id) ON DELETE RESTRICT,
    kind text NOT NULL CHECK (kind IN ('wave', 'final')),
    wave integer CHECK (wave >= 0),
    revision integer NOT NULL CHECK (revision >= 1),
    previous_snapshot_id text,
    row_digest text NOT NULL CHECK (row_digest ~ '^[0-9a-f]{64}$'),
    total_count integer NOT NULL CHECK (total_count >= 0),
    counts jsonb NOT NULL CHECK (jsonb_typeof(counts) = 'object'),
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (id, campaign_id),
    FOREIGN KEY (previous_snapshot_id, campaign_id)
        REFERENCES merlin_storage.campaign_frontier_completion_snapshots(id, campaign_id)
        ON DELETE RESTRICT,
    CHECK ((kind = 'wave') = (wave IS NOT NULL)),
    CHECK ((revision = 1) = (previous_snapshot_id IS NULL))
);

CREATE UNIQUE INDEX campaign_frontier_completion_snapshot_scope_idx
    ON merlin_storage.campaign_frontier_completion_snapshots
    (campaign_id, kind, COALESCE(wave, -1), revision);

CREATE INDEX campaign_frontier_completion_snapshot_current_idx
    ON merlin_storage.campaign_frontier_completion_snapshots
    (campaign_id, kind, COALESCE(wave, -1), revision DESC);

CREATE INDEX campaign_frontier_completion_snapshot_history_idx
    ON merlin_storage.campaign_frontier_completion_snapshots
    (campaign_id, sequence);

CREATE TABLE merlin_storage.campaign_frontier_completion_rows (
    snapshot_id text NOT NULL
        REFERENCES merlin_storage.campaign_frontier_completion_snapshots(id)
        ON DELETE RESTRICT,
    position integer NOT NULL CHECK (position >= 0),
    campaign_id text NOT NULL,
    candidate_id text NOT NULL CHECK (candidate_id <> ''),
    state text NOT NULL CHECK (state IN ('queued', 'claimed', 'scheduled', 'terminal')),
    outcome text NOT NULL CHECK (outcome IN (
        'confirmed', 'refuted', 'partial', 'unclear', 'blocked',
        'retryable', 'unattempted'
    )),
    attempt_id uuid,
    task_id text CHECK (task_id IS NULL OR task_id <> ''),
    evidence_refs text[] NOT NULL DEFAULT '{}',
    disposition_reason text CHECK (
        disposition_reason IS NULL OR disposition_reason <> ''
    ),
    PRIMARY KEY (snapshot_id, position),
    UNIQUE (snapshot_id, candidate_id),
    FOREIGN KEY (campaign_id, candidate_id)
        REFERENCES merlin_storage.campaign_frontier_candidates(campaign_id, id)
        ON DELETE RESTRICT,
    FOREIGN KEY (attempt_id, campaign_id)
        REFERENCES merlin_storage.campaign_technique_attempts(id, campaign_id)
        ON DELETE RESTRICT
);

CREATE INDEX campaign_frontier_completion_candidate_history_idx
    ON merlin_storage.campaign_frontier_completion_rows
    (campaign_id, candidate_id, snapshot_id);

CREATE TRIGGER campaign_frontier_completion_snapshots_append_only
    BEFORE UPDATE OR DELETE ON merlin_storage.campaign_frontier_completion_snapshots
    FOR EACH ROW EXECUTE FUNCTION merlin_storage.reject_observability_rewrite();

CREATE TRIGGER campaign_frontier_completion_rows_append_only
    BEFORE UPDATE OR DELETE ON merlin_storage.campaign_frontier_completion_rows
    FOR EACH ROW EXECUTE FUNCTION merlin_storage.reject_observability_rewrite();

COMMENT ON TABLE merlin_storage.campaign_frontier_completion_snapshots IS
    'Bounded immutable revision headers for exact wave or final frontier completion state.';
COMMENT ON TABLE merlin_storage.campaign_frontier_completion_rows IS
    'Ordered authoritative per-candidate state and outcome rows bound by the snapshot digest.';
