ALTER TABLE merlin_storage.campaign_hypothesis_events
    DROP CONSTRAINT campaign_hypothesis_events_infrastructure_failure_categor_check,
    ADD CONSTRAINT campaign_hypothesis_events_failure_category_check
        CHECK (infrastructure_failure_category IN (
            'adapter_timeout', 'missing_response_pointer', 'delivery_failure',
            'unhealthy_target_session', 'storage_exhausted'
        ));

ALTER TABLE merlin_storage.campaign_counter_events
    DROP CONSTRAINT campaign_counter_events_infrastructure_failure_category_check,
    ADD CONSTRAINT campaign_counter_events_infrastructure_failure_category_check
        CHECK (infrastructure_failure_category IN (
            'adapter_timeout', 'missing_response_pointer', 'delivery_failure',
            'unhealthy_target_session', 'storage_exhausted'
        ));
