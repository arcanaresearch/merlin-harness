ALTER TABLE merlin_storage.assessment_campaigns
    DROP CONSTRAINT assessment_campaigns_stop_reason_check;

ALTER TABLE merlin_storage.assessment_campaigns
    ADD CONSTRAINT assessment_campaigns_stop_reason_check CHECK (
        stop_reason IS NULL OR stop_reason IN (
            'saturated',
            'completed_with_limitations',
            'capped',
            'failed',
            'interrupted',
            'operator_stopped'
        )
    );
