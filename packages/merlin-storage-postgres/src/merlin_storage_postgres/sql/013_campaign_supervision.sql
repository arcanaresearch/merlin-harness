CREATE TABLE merlin_storage.campaign_waves (
    campaign_id text NOT NULL REFERENCES merlin_storage.assessment_campaigns(id) ON DELETE CASCADE,
    wave integer NOT NULL CHECK (wave >= 0),
    kind text NOT NULL CHECK (kind IN ('initial', 'followup', 'validation')),
    schedule_id uuid NOT NULL REFERENCES merlin_storage.scheduler_schedules(id),
    status text NOT NULL CHECK (status IN ('running', 'completed', 'failed', 'cancelled')),
    summary jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (campaign_id, wave)
);

CREATE TABLE merlin_storage.campaign_lead_journal (
    campaign_id text NOT NULL REFERENCES merlin_storage.assessment_campaigns(id) ON DELETE CASCADE,
    sequence bigint GENERATED ALWAYS AS IDENTITY,
    wave integer NOT NULL CHECK (wave >= 0),
    payload jsonb NOT NULL,
    payload_digest text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (campaign_id, sequence)
);
