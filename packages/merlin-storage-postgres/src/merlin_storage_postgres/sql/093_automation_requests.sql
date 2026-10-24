CREATE TABLE merlin_storage.automation_requests (
    campaign_id text NOT NULL REFERENCES merlin_storage.assessment_campaigns(id),
    request_id text NOT NULL CHECK (btrim(request_id) <> ''),
    decision_id text NOT NULL,
    wave integer NOT NULL,
    run_id text NOT NULL UNIQUE,
    payload jsonb NOT NULL CHECK (jsonb_typeof(payload) = 'object'),
    payload_digest text NOT NULL CHECK (payload_digest ~ '^sha256:[0-9a-f]{64}$'),
    state text NOT NULL DEFAULT 'planned' CHECK (
        state IN ('planned', 'claimed', 'running', 'completed', 'retry_wait', 'rejected')
    ),
    claim_id text,
    lease_expires_at timestamptz,
    attempt_count integer NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
    next_retry_at timestamptz,
    outcome jsonb CHECK (jsonb_typeof(outcome) = 'object'),
    failure text,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (campaign_id, request_id),
    CHECK ((state IN ('claimed', 'running')) =
           (claim_id IS NOT NULL AND lease_expires_at IS NOT NULL)),
    CHECK ((state = 'retry_wait') = (next_retry_at IS NOT NULL)),
    CHECK ((state IN ('completed', 'rejected')) = (outcome IS NOT NULL))
);

CREATE INDEX automation_requests_due_idx
    ON merlin_storage.automation_requests (campaign_id, created_at)
    WHERE state IN ('planned', 'claimed', 'running', 'retry_wait');

COMMENT ON TABLE merlin_storage.automation_requests IS
    'One durable row per committed Lead automation request; campaign/request is its idempotency key.';
