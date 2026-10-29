CREATE TABLE merlin_storage.target_admission_latches (
    campaign_id text NOT NULL
        REFERENCES merlin_storage.assessment_campaigns(id) ON DELETE CASCADE,
    session_id uuid NOT NULL
        REFERENCES merlin_storage.sessions(id) ON DELETE CASCADE,
    target_id text NOT NULL CHECK (target_id <> ''),
    target_session_id text NOT NULL
        CHECK (target_session_id = '' OR length(target_session_id) <= 256),
    state text NOT NULL CHECK (
        state IN ('open', 'latched', 'activated', 'resolved', 'overflow')
    ),
    revision bigint NOT NULL DEFAULT 0 CHECK (revision >= 0),
    transition_count bigint NOT NULL DEFAULT 0 CHECK (transition_count >= 0),
    latched_session_count integer NOT NULL DEFAULT 0
        CHECK (latched_session_count >= 0),
    pending_event_digests text[] NOT NULL DEFAULT '{}',
    pending_event_count integer NOT NULL DEFAULT 0 CHECK (pending_event_count >= 0),
    event_set_digest text,
    generation_id text,
    start_decision_id text,
    settlement_id text,
    lead_decision_id text,
    authorized_schedule_id uuid,
    current_surface_digest text,
    compiled_adapter_digest text,
    reviewed_adapter_digest text,
    authorized_assignment_tools jsonb NOT NULL DEFAULT '{}'::jsonb
        CHECK (jsonb_typeof(authorized_assignment_tools) = 'object'),
    lane_schedule_id uuid,
    lane_node_id text,
    lane_assignment_id text,
    lane_attempt_id uuid,
    lane_generation integer CHECK (lane_generation IS NULL OR lane_generation > 0),
    lane_recovery_required boolean NOT NULL DEFAULT false,
    lane_recovery_root_attempt_id uuid,
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (campaign_id, target_id, target_session_id),
    CONSTRAINT target_admission_latch_scope CHECK (
        (target_session_id = '' AND state IN ('open', 'latched'))
        OR target_session_id <> ''
    ),
    CONSTRAINT target_admission_latch_pending CHECK (
        cardinality(pending_event_digests) <= 256
        AND pending_event_count >= cardinality(pending_event_digests)
    ),
    CONSTRAINT target_admission_latch_lane CHECK (
        (lane_schedule_id IS NULL AND lane_node_id IS NULL
         AND lane_assignment_id IS NULL AND lane_attempt_id IS NULL
         AND lane_generation IS NULL AND lane_recovery_required = false
         AND lane_recovery_root_attempt_id IS NULL)
        OR
        (lane_schedule_id IS NOT NULL AND lane_node_id IS NOT NULL
         AND lane_assignment_id IS NOT NULL AND lane_attempt_id IS NOT NULL
         AND lane_generation IS NOT NULL)
    )
);

CREATE UNIQUE INDEX target_admission_latches_lane_attempt_idx
    ON merlin_storage.target_admission_latches (lane_attempt_id)
    WHERE lane_attempt_id IS NOT NULL;

CREATE TABLE merlin_storage.target_effect_permits (
    permit_digest text PRIMARY KEY,
    campaign_id text NOT NULL
        REFERENCES merlin_storage.assessment_campaigns(id) ON DELETE CASCADE,
    session_id uuid NOT NULL
        REFERENCES merlin_storage.sessions(id) ON DELETE CASCADE,
    target_id text NOT NULL CHECK (target_id <> ''),
    target_session_id text NOT NULL
        CHECK (target_session_id <> '' AND length(target_session_id) <= 256),
    schedule_id uuid NOT NULL
        REFERENCES merlin_storage.scheduler_schedules(id) ON DELETE RESTRICT,
    node_id text NOT NULL CHECK (node_id <> ''),
    assignment_id text NOT NULL CHECK (assignment_id <> ''),
    attempt_id uuid NOT NULL
        REFERENCES merlin_storage.scheduler_attempts(id) ON DELETE RESTRICT,
    generation integer NOT NULL CHECK (generation > 0),
    call_id text NOT NULL CHECK (call_id <> ''),
    tool_name text NOT NULL CHECK (tool_name <> ''),
    effect text NOT NULL CHECK (effect <> ''),
    arguments_digest text NOT NULL CHECK (arguments_digest <> ''),
    arguments_payload jsonb,
    idempotency_key text NOT NULL UNIQUE
        REFERENCES merlin_storage.tool_effects(idempotency_key) ON DELETE RESTRICT,
    state text NOT NULL CHECK (state IN ('intent', 'adopted', 'terminal', 'revoked')),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (attempt_id, call_id, tool_name)
);

CREATE INDEX target_effect_permits_attempt_state_idx
    ON merlin_storage.target_effect_permits (attempt_id, state);

-- Old daemons can still try to append v1 observations after this migration.
-- Reject every fresh v1 append at the table boundary, including while the
-- authenticated upgrader is in flight. Only the exact replay that
-- CampaignStore uses for an already-durable decision may pass through to its
-- ON CONFLICT branch; it cannot introduce a new journal row.
CREATE FUNCTION merlin_storage.guard_legacy_target_transition_insert()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF NEW.payload ->> 'kind' = 'target_surface_transition_observed'
       AND NEW.payload ->> 'schema' =
           'merlin.target-surface-transition-observed.v1' THEN
        IF NEW.decision_id IS NOT NULL AND EXISTS (
            SELECT 1
            FROM merlin_storage.campaign_lead_journal AS existing
            WHERE existing.campaign_id = NEW.campaign_id
              AND existing.decision_id = NEW.decision_id
              AND existing.payload = NEW.payload
              AND existing.payload_digest = NEW.payload_digest
        ) THEN
            RETURN NEW;
        END IF;

        RAISE EXCEPTION USING
            ERRCODE = '23514',
            MESSAGE = 'legacy target transition observations are read-only after migration';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER campaign_lead_journal_legacy_transition_insert_guard
BEFORE INSERT ON merlin_storage.campaign_lead_journal
FOR EACH ROW
EXECUTE FUNCTION merlin_storage.guard_legacy_target_transition_insert();

-- Fail closed across upgrades. These rows materialize only the immutable,
-- canonically named transition observations already present in the Lead
-- journal. The Python authority boundary revalidates the bounded event set
-- before it admits a passive refresh or writes an event-set digest.
WITH observed AS (
    SELECT journal.campaign_id,
           campaign.session_id,
           journal.payload ->> 'target_id' AS target_id,
           journal.payload ->> 'target_session_id' AS target_session_id,
           journal.payload ->> 'event_digest' AS event_digest,
           journal.sequence
    FROM merlin_storage.campaign_lead_journal AS journal
    JOIN merlin_storage.assessment_campaigns AS campaign
      ON campaign.id = journal.campaign_id
    WHERE journal.payload ->> 'kind' = 'target_surface_transition_observed'
      AND journal.payload ->> 'schema' =
          'merlin.target-surface-transition-observed.v2'
      AND journal.payload ->> 'campaign_id' = journal.campaign_id
      AND journal.payload ->> 'session_id' = campaign.session_id::text
      AND journal.payload ->> 'target_id' = coalesce(
          campaign.manifest #>> '{manifest,target,id}',
          campaign.manifest #>> '{target,id}'
      )
      AND journal.payload ->> 'target_session_id' <> ''
      AND journal.payload ->> 'event_digest' ~ '^sha256:[0-9a-f]{64}$'
      AND journal.decision_id = 'target-surface-transition-observed/'
          || substring(journal.payload ->> 'event_digest' FROM 8)
), grouped AS (
    SELECT campaign_id, session_id, target_id, target_session_id,
           count(*)::integer AS event_count,
           (array_agg(event_digest ORDER BY sequence))[1:256] AS event_digests
    FROM observed
    GROUP BY campaign_id, session_id, target_id, target_session_id
)
INSERT INTO merlin_storage.target_admission_latches
       (campaign_id, session_id, target_id, target_session_id, state,
        revision, transition_count, pending_event_digests, pending_event_count)
SELECT campaign_id, session_id, target_id, target_session_id,
       CASE WHEN event_count > 256 THEN 'overflow' ELSE 'latched' END,
       1, event_count, event_digests, event_count
FROM grouped
ON CONFLICT (campaign_id, target_id, target_session_id) DO NOTHING;

WITH session_latches AS (
    SELECT campaign_id, session_id, target_id,
           sum(transition_count) AS transition_count,
           count(*)::integer AS latched_session_count
    FROM merlin_storage.target_admission_latches
    WHERE target_session_id <> '' AND state IN ('latched', 'overflow')
    GROUP BY campaign_id, session_id, target_id
), legacy AS (
    SELECT journal.campaign_id,
           campaign.session_id,
           journal.payload ->> 'target_id' AS target_id,
           count(*)::bigint AS transition_count
    FROM merlin_storage.campaign_lead_journal AS journal
    JOIN merlin_storage.assessment_campaigns AS campaign
      ON campaign.id = journal.campaign_id
    WHERE journal.payload ->> 'kind' = 'target_surface_transition_observed'
      AND journal.payload ->> 'schema' =
          'merlin.target-surface-transition-observed.v1'
      AND journal.payload ->> 'campaign_id' = journal.campaign_id
      AND journal.payload ->> 'session_id' = campaign.session_id::text
      AND journal.payload ->> 'target_id' = coalesce(
          campaign.manifest #>> '{manifest,target,id}',
          campaign.manifest #>> '{target,id}'
      )
      AND journal.payload ->> 'event_digest' ~ '^sha256:[0-9a-f]{64}$'
      AND journal.decision_id = 'target-surface-transition-observed/'
          || substring(journal.payload ->> 'event_digest' FROM 8)
    GROUP BY journal.campaign_id, campaign.session_id,
             journal.payload ->> 'target_id'
), target_scopes AS (
    SELECT campaign_id, session_id, target_id FROM session_latches
    UNION
    SELECT campaign_id, session_id, target_id FROM legacy
), head_values AS (
    SELECT scope.campaign_id,
           scope.session_id,
           scope.target_id,
           coalesce(session_latches.transition_count, 0)
               + coalesce(legacy.transition_count, 0) AS transition_count,
           coalesce(session_latches.latched_session_count, 0)
               + CASE WHEN legacy.transition_count > 0 THEN 1 ELSE 0 END
                   AS latched_session_count
    FROM target_scopes AS scope
    LEFT JOIN session_latches USING (campaign_id, session_id, target_id)
    LEFT JOIN legacy USING (campaign_id, session_id, target_id)
)
INSERT INTO merlin_storage.target_admission_latches
       (campaign_id, session_id, target_id, target_session_id, state,
        revision, transition_count, latched_session_count)
SELECT campaign_id, session_id, target_id, '', 'latched',
       1, transition_count, latched_session_count
FROM head_values
ON CONFLICT (campaign_id, target_id, target_session_id) DO UPDATE SET
    session_id = EXCLUDED.session_id,
    state = EXCLUDED.state,
    revision = merlin_storage.target_admission_latches.revision + 1,
    transition_count = EXCLUDED.transition_count,
    latched_session_count = EXCLUDED.latched_session_count,
    updated_at = now();
