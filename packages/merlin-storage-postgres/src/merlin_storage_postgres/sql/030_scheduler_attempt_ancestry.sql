ALTER TABLE merlin_storage.scheduler_attempts
    ADD CONSTRAINT scheduler_attempts_schedule_id_id_key UNIQUE (schedule_id, id),
    ADD COLUMN target_session_id text
        CHECK (target_session_id IS NULL OR btrim(target_session_id) <> '');

ALTER TABLE merlin_storage.scheduler_schedules
    ADD COLUMN parent_schedule_id uuid,
    ADD COLUMN parent_attempt_id uuid,
    ADD CONSTRAINT scheduler_schedules_parent_pair
        CHECK ((parent_schedule_id IS NULL) = (parent_attempt_id IS NULL)),
    ADD CONSTRAINT scheduler_schedules_parent_attempt_fkey
        FOREIGN KEY (parent_schedule_id, parent_attempt_id)
        REFERENCES merlin_storage.scheduler_attempts(schedule_id, id)
        ON DELETE RESTRICT;

CREATE INDEX scheduler_schedules_parent_attempt_idx
    ON merlin_storage.scheduler_schedules (parent_schedule_id, parent_attempt_id)
    WHERE parent_attempt_id IS NOT NULL;
