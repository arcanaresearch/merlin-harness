ALTER TABLE merlin_storage.scheduler_attempts
    ADD COLUMN failure_kind text
    CHECK (failure_kind IS NULL OR failure_kind IN ('cleanup_unresolved'));
