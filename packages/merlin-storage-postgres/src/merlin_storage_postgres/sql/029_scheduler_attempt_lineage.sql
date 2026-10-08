ALTER TABLE merlin_storage.scheduler_attempts
    ADD COLUMN replaced_attempt_id uuid
        REFERENCES merlin_storage.scheduler_attempts(id) ON DELETE RESTRICT,
    ADD CONSTRAINT scheduler_attempts_replacement_not_self
        CHECK (replaced_attempt_id IS NULL OR replaced_attempt_id <> id);

CREATE INDEX scheduler_attempts_replacement_idx
    ON merlin_storage.scheduler_attempts (replaced_attempt_id)
    WHERE replaced_attempt_id IS NOT NULL;
