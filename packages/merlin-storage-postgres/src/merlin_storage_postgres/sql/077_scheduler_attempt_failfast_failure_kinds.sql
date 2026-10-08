ALTER TABLE merlin_storage.scheduler_attempts
    DROP CONSTRAINT IF EXISTS scheduler_attempts_failure_kind_check;

ALTER TABLE merlin_storage.scheduler_attempts
    ADD CONSTRAINT scheduler_attempts_failure_kind_check
    CHECK (
        failure_kind IS NULL
        OR failure_kind IN (
            'adapter_timeout',
            'authorization',
            'budget',
            'cleanup_unresolved',
            'configuration',
            'missing_target_effect',
            'pointer_repair',
            'provider_transient',
            'provider_truncated',
            'result_schema_invalid',
            'scope',
            'state_integrity',
            'target_adapter_unhealthy',
            'transport_transient',
            'unknown',
            'worker_interrupted'
        )
    );
