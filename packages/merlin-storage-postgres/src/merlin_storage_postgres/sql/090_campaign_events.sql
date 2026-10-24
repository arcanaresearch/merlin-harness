ALTER TABLE merlin_storage.assessment_campaigns
    ADD COLUMN event_count bigint NOT NULL DEFAULT 0 CHECK (event_count >= 0),
    ADD COLUMN last_event_digest text NOT NULL DEFAULT repeat('0', 64)
        CHECK (length(last_event_digest) = 64),
    ADD COLUMN pause_cause text CHECK (
        pause_cause IN (
            'operator',
            'daemon_shutdown',
            'provider_billing',
            'circuit_breaker',
            'recovery',
            'storage_exhausted',
            'unattributed'
        )
    );

CREATE TABLE merlin_storage.campaign_events (
    campaign_id text NOT NULL
        REFERENCES merlin_storage.assessment_campaigns(id) ON DELETE CASCADE,
    sequence bigint NOT NULL CHECK (sequence > 0),
    type text NOT NULL CHECK (type IN ('campaign_paused', 'campaign_resumed')),
    cause text NOT NULL CHECK (
        cause IN (
            'operator',
            'daemon_shutdown',
            'provider_billing',
            'circuit_breaker',
            'recovery',
            'storage_exhausted',
            'unattributed'
        )
    ),
    command_id text NOT NULL CHECK (command_id <> ''),
    payload jsonb NOT NULL CHECK (jsonb_typeof(payload) = 'object'),
    previous_digest text NOT NULL CHECK (length(previous_digest) = 64),
    digest text NOT NULL CHECK (length(digest) = 64),
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (campaign_id, sequence),
    UNIQUE (campaign_id, command_id),
    UNIQUE (campaign_id, digest)
);

CREATE FUNCTION merlin_storage.reject_campaign_event_rewrite() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'campaign events are append-only';
END;
$$;

CREATE TRIGGER campaign_events_append_only
    BEFORE UPDATE OR DELETE ON merlin_storage.campaign_events
    FOR EACH ROW EXECUTE FUNCTION merlin_storage.reject_campaign_event_rewrite();

COMMENT ON TABLE merlin_storage.campaign_events IS
    'One immutable, hash-chained row per committed campaign lifecycle transition.';

COMMENT ON COLUMN merlin_storage.assessment_campaigns.pause_cause IS
    'Operator-visible cause for a paused campaign; lifecycle_pause_reason remains the automatic reconciliation gate.';
