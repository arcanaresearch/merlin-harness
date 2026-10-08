ALTER TABLE merlin_storage.agent_tasks
    DROP CONSTRAINT agent_tasks_state_check;

ALTER TABLE merlin_storage.agent_tasks
    ADD CONSTRAINT agent_tasks_state_check CHECK (
        state IN ('queued', 'running', 'completed', 'failed', 'interrupted', 'cancelled')
    );
