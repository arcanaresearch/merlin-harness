CREATE INDEX tool_effects_campaign_technique_attempt_target_idx
    ON merlin_storage.tool_effects (
        campaign_id,
        ((result_metadata ->> 'technique_attempt_id'))
    )
    WHERE result_metadata ->> 'interaction_role' = 'target_exchange'
      AND state IN ('acknowledged', 'reconciled');

COMMENT ON INDEX merlin_storage.tool_effects_campaign_technique_attempt_target_idx IS
    'Supports conservative A2A history projection onto exact durable target effects.';
