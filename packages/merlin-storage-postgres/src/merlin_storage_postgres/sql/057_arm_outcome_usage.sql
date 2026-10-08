ALTER TABLE merlin_storage.campaign_arm_outcomes
    ADD COLUMN usage jsonb NOT NULL DEFAULT '{}'::jsonb;

COMMENT ON COLUMN merlin_storage.campaign_arm_outcomes.usage IS
    'Provider-reported or explicitly estimated token usage for this exact ARM execution.';
