CREATE INDEX IF NOT EXISTS campaign_waves_schedule_id_idx
    ON merlin_storage.campaign_waves (schedule_id, campaign_id);

CREATE INDEX IF NOT EXISTS campaign_technique_attempt_snapshot_idx
    ON merlin_storage.campaign_technique_attempts
       (campaign_id, task_id, technique_id, strategy_id, language, transform_id, updated_at DESC);
