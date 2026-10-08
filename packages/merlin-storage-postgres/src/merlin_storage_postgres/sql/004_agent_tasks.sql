CREATE TABLE merlin_storage.agent_tasks (
    id uuid PRIMARY KEY,
    owner text NOT NULL CHECK (owner <> ''),
    role text NOT NULL CHECK (role <> ''),
    description text NOT NULL CHECK (description <> ''),
    provider text NOT NULL CHECK (provider <> ''),
    model text NOT NULL CHECK (model <> ''),
    state text NOT NULL CHECK (
        state IN ('queued', 'running', 'completed', 'failed', 'interrupted')
    ),
    result text,
    error text,
    latest_event text,
    created_at timestamptz NOT NULL DEFAULT now(),
    started_at timestamptz,
    completed_at timestamptz
);

CREATE INDEX agent_tasks_state_created_idx
    ON merlin_storage.agent_tasks (state, created_at, id);

CREATE TABLE merlin_storage.agent_task_events (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    task_id uuid NOT NULL REFERENCES merlin_storage.agent_tasks(id) ON DELETE CASCADE,
    sequence integer NOT NULL CHECK (sequence >= 0),
    type text NOT NULL CHECK (type <> ''),
    content text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (task_id, sequence)
);

CREATE INDEX agent_task_events_task_order_idx
    ON merlin_storage.agent_task_events (task_id, sequence);
