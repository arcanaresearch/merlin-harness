ALTER TABLE merlin_storage.assessment_campaigns
    DROP CONSTRAINT assessment_campaigns_lifecycle_pause_reason_check;

ALTER TABLE merlin_storage.assessment_campaigns
    ADD CONSTRAINT assessment_campaigns_lifecycle_pause_reason_check
    CHECK (lifecycle_pause_reason IN (
        'fixed_forward',
        'quota',
        'acknowledged_settlement',
        'cleanup',
        'recovery_incompatible'
    ));

COMMENT ON COLUMN merlin_storage.assessment_campaigns.lifecycle_pause_reason IS
    'Automatic reconciliation gate. recovery_incompatible quarantines sealed state that this runtime cannot safely restore.';
