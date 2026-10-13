ALTER TABLE merlin_storage.assessment_campaigns
    DROP CONSTRAINT assessment_campaigns_stop_reason_check;

UPDATE merlin_storage.assessment_campaigns
SET stop_reason = CASE stop_reason
    WHEN 'saturated' THEN 'frontier_exhausted'
    WHEN 'completed_with_limitations' THEN 'frontier_exhausted'
    WHEN 'coverage_incomplete' THEN 'budget_exhausted'
    WHEN 'capped' THEN 'budget_exhausted'
    WHEN 'failed' THEN 'infrastructure_blocked_after_retry_policy'
    WHEN 'interrupted' THEN 'infrastructure_blocked_after_retry_policy'
    ELSE stop_reason
END
WHERE stop_reason IS NOT NULL;

ALTER TABLE merlin_storage.assessment_campaigns
    ADD CONSTRAINT assessment_campaigns_stop_reason_check CHECK (
        stop_reason IS NULL OR stop_reason IN (
            'objective_satisfied',
            'frontier_exhausted',
            'budget_exhausted',
            'target_terminal',
            'operator_stopped',
            'infrastructure_blocked_after_retry_policy'
        )
    );

COMMENT ON COLUMN merlin_storage.assessment_campaigns.stop_reason IS
    'Closed merlin.campaign-terminal.v1 reason; migrations translate legacy values explicitly.';
