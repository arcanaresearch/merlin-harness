CREATE TABLE merlin_storage.scheduler_schedules (
    id uuid PRIMARY KEY,
    session_id uuid NOT NULL REFERENCES merlin_storage.sessions(id) ON DELETE CASCADE,
    graph jsonb NOT NULL,
    graph_digest text NOT NULL CHECK (length(graph_digest) = 64),
    checkpoint jsonb NOT NULL,
    checkpoint_digest text NOT NULL CHECK (length(checkpoint_digest) = 64),
    event_count bigint NOT NULL DEFAULT 0 CHECK (event_count >= 0),
    last_event_digest text NOT NULL CHECK (length(last_event_digest) = 64),
    status text NOT NULL CHECK (status IN ('running', 'completed', 'failed', 'cancelled')),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE merlin_storage.scheduler_nodes (
    schedule_id uuid NOT NULL REFERENCES merlin_storage.scheduler_schedules(id) ON DELETE CASCADE,
    node_id text NOT NULL CHECK (node_id <> ''),
    state text NOT NULL CHECK (
        state IN ('pending', 'ready', 'leased', 'completed', 'failed', 'cancelled', 'quarantined')
    ),
    lease_generation integer NOT NULL DEFAULT 0 CHECK (lease_generation >= 0),
    attempts_started integer NOT NULL DEFAULT 0 CHECK (attempts_started >= 0),
    failed_attempts integer NOT NULL DEFAULT 0 CHECK (failed_attempts >= 0),
    result_digest text,
    PRIMARY KEY (schedule_id, node_id)
);

CREATE TABLE merlin_storage.scheduler_attempts (
    id uuid PRIMARY KEY,
    schedule_id uuid NOT NULL,
    node_id text NOT NULL,
    generation integer NOT NULL CHECK (generation > 0),
    owner text NOT NULL CHECK (owner <> ''),
    state text NOT NULL CHECK (
        state IN ('running', 'completed', 'failed', 'expired', 'cancelled', 'quarantined')
    ),
    lease_expires_at timestamptz NOT NULL,
    heartbeat_at timestamptz NOT NULL,
    started_at timestamptz NOT NULL DEFAULT now(),
    completed_at timestamptz,
    error text,
    UNIQUE (schedule_id, node_id, generation),
    FOREIGN KEY (schedule_id, node_id)
        REFERENCES merlin_storage.scheduler_nodes(schedule_id, node_id) ON DELETE CASCADE
);

CREATE TABLE merlin_storage.scheduler_events (
    schedule_id uuid NOT NULL REFERENCES merlin_storage.scheduler_schedules(id) ON DELETE CASCADE,
    sequence bigint NOT NULL CHECK (sequence > 0),
    type text NOT NULL CHECK (type <> ''),
    payload jsonb NOT NULL,
    previous_digest text NOT NULL CHECK (length(previous_digest) = 64),
    digest text NOT NULL CHECK (length(digest) = 64),
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (schedule_id, sequence),
    UNIQUE (schedule_id, digest)
);

CREATE TABLE merlin_storage.scheduler_results (
    attempt_id uuid PRIMARY KEY REFERENCES merlin_storage.scheduler_attempts(id) ON DELETE RESTRICT,
    schedule_id uuid NOT NULL,
    node_id text NOT NULL,
    generation integer NOT NULL,
    schema_id text NOT NULL CHECK (schema_id <> ''),
    payload jsonb NOT NULL,
    payload_digest text NOT NULL CHECK (length(payload_digest) = 64),
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (schedule_id, node_id, generation),
    FOREIGN KEY (schedule_id, node_id, generation)
        REFERENCES merlin_storage.scheduler_attempts(schedule_id, node_id, generation)
);

CREATE TABLE merlin_storage.scheduler_mailbox (
    id uuid PRIMARY KEY,
    schedule_id uuid NOT NULL REFERENCES merlin_storage.scheduler_schedules(id) ON DELETE CASCADE,
    sender text NOT NULL CHECK (sender <> ''),
    recipient text NOT NULL CHECK (recipient <> ''),
    schema_id text NOT NULL CHECK (schema_id <> ''),
    payload jsonb NOT NULL,
    payload_digest text NOT NULL CHECK (length(payload_digest) = 64),
    claimed_by_attempt uuid REFERENCES merlin_storage.scheduler_attempts(id),
    claimed_generation integer,
    created_at timestamptz NOT NULL DEFAULT now(),
    acknowledged_at timestamptz,
    CHECK ((claimed_by_attempt IS NULL) = (claimed_generation IS NULL))
);

CREATE INDEX scheduler_mailbox_delivery_idx
    ON merlin_storage.scheduler_mailbox (schedule_id, recipient, acknowledged_at, created_at);

CREATE TABLE merlin_storage.scheduler_completions (
    attempt_id uuid PRIMARY KEY REFERENCES merlin_storage.scheduler_attempts(id) ON DELETE RESTRICT,
    schedule_id uuid NOT NULL REFERENCES merlin_storage.scheduler_schedules(id) ON DELETE CASCADE,
    node_id text NOT NULL,
    result_digest text NOT NULL CHECK (length(result_digest) = 64),
    published_at timestamptz NOT NULL DEFAULT now(),
    acknowledged_at timestamptz
);
