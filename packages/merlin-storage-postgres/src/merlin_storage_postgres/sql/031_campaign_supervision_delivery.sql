ALTER TABLE merlin_storage.campaign_worker_messages
    ADD COLUMN kind text NOT NULL DEFAULT 'discovery'
        CHECK (kind IN ('discovery', 'blocker', 'decline', 'reroute_request')),
    ADD COLUMN related_task_id text,
    ADD COLUMN requested_profile_id text,
    ADD COLUMN lead_claim_id uuid,
    ADD COLUMN lead_claimed_by text,
    ADD COLUMN lead_claim_generation integer NOT NULL DEFAULT 0 CHECK (lead_claim_generation >= 0),
    ADD COLUMN lead_claim_expires_at timestamptz,
    ADD CONSTRAINT campaign_worker_message_claim_complete CHECK (
        (lead_claim_id IS NULL AND lead_claimed_by IS NULL AND lead_claim_expires_at IS NULL)
        OR
        (lead_claim_id IS NOT NULL AND lead_claimed_by IS NOT NULL AND lead_claim_expires_at IS NOT NULL)
    );

ALTER TABLE merlin_storage.campaign_lead_journal
    ADD COLUMN decision_id text;

CREATE UNIQUE INDEX campaign_lead_journal_decision_idx
    ON merlin_storage.campaign_lead_journal (campaign_id, decision_id)
    WHERE decision_id IS NOT NULL;

CREATE TABLE merlin_storage.campaign_lead_directives (
    id text PRIMARY KEY,
    campaign_id text NOT NULL
        REFERENCES merlin_storage.assessment_campaigns(id) ON DELETE CASCADE,
    wave integer NOT NULL CHECK (wave >= 0),
    decision_id text NOT NULL,
    ordinal integer NOT NULL CHECK (ordinal >= 0),
    action text NOT NULL CHECK (action IN ('redirect', 'handoff', 'cancel')),
    source_node_id text NOT NULL,
    destination_node_id text,
    source_message_ids text[] NOT NULL DEFAULT '{}',
    payload jsonb NOT NULL,
    payload_digest text NOT NULL CHECK (payload_digest LIKE 'sha256:%'),
    state text NOT NULL DEFAULT 'planned' CHECK (state IN ('planned', 'applied', 'failed')),
    effect_reference text,
    failure text,
    created_at timestamptz NOT NULL DEFAULT now(),
    applied_at timestamptz,
    updated_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT campaign_lead_directive_receipt_complete CHECK (
        (state = 'planned' AND effect_reference IS NULL AND failure IS NULL AND applied_at IS NULL)
        OR (state = 'failed' AND failure IS NOT NULL AND applied_at IS NULL)
        OR (state = 'applied' AND effect_reference IS NOT NULL AND failure IS NULL
            AND applied_at IS NOT NULL)
    ),
    UNIQUE (campaign_id, decision_id, ordinal)
);

CREATE INDEX campaign_lead_directives_unapplied_idx
    ON merlin_storage.campaign_lead_directives (campaign_id, created_at, id)
    WHERE state IN ('planned', 'failed');
