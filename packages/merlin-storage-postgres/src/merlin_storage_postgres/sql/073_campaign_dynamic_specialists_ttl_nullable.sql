ALTER TABLE merlin_storage.campaign_dynamic_specialists
    ALTER COLUMN ttl_waves DROP NOT NULL;

ALTER TABLE merlin_storage.campaign_dynamic_specialists
    DROP CONSTRAINT IF EXISTS campaign_dynamic_specialists_ttl_waves_check;

ALTER TABLE merlin_storage.campaign_dynamic_specialists
    ADD CONSTRAINT campaign_dynamic_specialists_ttl_waves_check
    CHECK (ttl_waves IS NULL OR ttl_waves >= 1);
