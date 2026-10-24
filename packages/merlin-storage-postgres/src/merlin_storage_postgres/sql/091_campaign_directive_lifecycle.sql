ALTER TABLE merlin_storage.campaign_lead_directives
    ADD COLUMN attempt_count integer NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
    ADD COLUMN next_retry_at timestamptz,
    ADD COLUMN failure_kind text CHECK (
        failure_kind IN ('validation', 'unknown_worker', 'runtime', 'transport', 'retry_exhausted')
    );

ALTER TABLE merlin_storage.campaign_lead_directives
    DROP CONSTRAINT campaign_lead_directives_state_check,
    DROP CONSTRAINT campaign_lead_directive_receipt_complete;

UPDATE merlin_storage.campaign_lead_directives
SET state = 'retry_wait',
    attempt_count = 1,
    failure_kind = 'runtime',
    next_retry_at = now(),
    updated_at = now()
WHERE state = 'failed';

ALTER TABLE merlin_storage.campaign_lead_directives
    ADD CONSTRAINT campaign_lead_directives_state_check CHECK (
        state IN ('planned', 'retry_wait', 'rejected', 'applied')
    ),
    ADD CONSTRAINT campaign_lead_directive_receipt_complete CHECK (
        (
            state = 'planned'
            AND attempt_count = 0
            AND effect_reference IS NULL
            AND failure IS NULL
            AND failure_kind IS NULL
            AND next_retry_at IS NULL
            AND applied_at IS NULL
        )
        OR (
            state = 'retry_wait'
            AND attempt_count > 0
            AND effect_reference IS NULL
            AND failure IS NOT NULL
            AND failure_kind IN ('runtime', 'transport')
            AND next_retry_at IS NOT NULL
            AND applied_at IS NULL
        )
        OR (
            state = 'rejected'
            AND attempt_count > 0
            AND effect_reference IS NULL
            AND failure IS NOT NULL
            AND failure_kind IN ('validation', 'unknown_worker', 'retry_exhausted')
            AND next_retry_at IS NULL
            AND applied_at IS NULL
        )
        OR (
            state = 'applied'
            AND effect_reference IS NOT NULL
            AND failure IS NULL
            AND failure_kind IS NULL
            AND next_retry_at IS NULL
            AND applied_at IS NOT NULL
        )
    );

DROP INDEX merlin_storage.campaign_lead_directives_unapplied_idx;

CREATE INDEX campaign_lead_directives_ready_idx
    ON merlin_storage.campaign_lead_directives
        (campaign_id, next_retry_at, created_at, id)
    WHERE state IN ('planned', 'retry_wait');
