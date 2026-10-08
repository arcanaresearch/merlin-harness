ALTER TABLE merlin_storage.findings
    ADD COLUMN identity text,
    ADD COLUMN target_id text NOT NULL DEFAULT 'unspecified',
    ADD COLUMN technique_id text NOT NULL DEFAULT 'unspecified',
    ADD COLUMN validation text NOT NULL DEFAULT 'unvalidated'
        CHECK (validation IN ('unvalidated', 'validated', 'refuted', 'inconclusive')),
    ADD COLUMN validation_summary text,
    ADD COLUMN witnesses jsonb NOT NULL DEFAULT '[]'::jsonb,
    ADD COLUMN attack_path jsonb NOT NULL DEFAULT '[]'::jsonb;

UPDATE merlin_storage.findings SET identity = 'legacy:' || id::text WHERE identity IS NULL;
ALTER TABLE merlin_storage.findings ALTER COLUMN identity SET NOT NULL;
CREATE UNIQUE INDEX findings_session_identity_idx
    ON merlin_storage.findings (session_id, identity);

ALTER TABLE merlin_storage.assessment_campaigns
    ADD COLUMN stop_reason text CHECK (
        stop_reason IS NULL OR stop_reason IN
            ('saturated', 'capped', 'failed', 'interrupted', 'operator_stopped')
    );

CREATE TABLE merlin_storage.campaign_observations (
    id text PRIMARY KEY,
    campaign_id text NOT NULL REFERENCES merlin_storage.assessment_campaigns(id) ON DELETE CASCADE,
    task_id text NOT NULL,
    target_id text NOT NULL CHECK (target_id <> ''),
    technique_id text NOT NULL CHECK (technique_id <> ''),
    outcome text NOT NULL CHECK (outcome IN ('observed', 'not_observed', 'inconclusive', 'error')),
    absence_authority text NOT NULL DEFAULT 'none'
        CHECK (absence_authority IN ('none', 'bounded', 'exhaustive')),
    summary text NOT NULL CHECK (summary <> ''),
    coverage text,
    witnesses jsonb NOT NULL DEFAULT '[]'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (campaign_id, task_id, target_id, technique_id)
);

CREATE INDEX campaign_observations_campaign_created_idx
    ON merlin_storage.campaign_observations (campaign_id, created_at, id);
