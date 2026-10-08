CREATE TABLE merlin_storage.target_profiles (
    id text PRIMARY KEY,
    title text NOT NULL CHECK (title <> ''),
    base_url text NOT NULL CHECK (base_url <> ''),
    model text NOT NULL CHECK (model <> '')
);

CREATE TABLE merlin_storage.pack_runs (
    id uuid PRIMARY KEY,
    session_id uuid NOT NULL REFERENCES merlin_storage.sessions(id) ON DELETE CASCADE,
    pack_id text NOT NULL CHECK (pack_id <> ''),
    case_id text NOT NULL CHECK (case_id <> ''),
    target_profile_id text NOT NULL REFERENCES merlin_storage.target_profiles(id),
    status text NOT NULL CHECK (
        status IN ('pending', 'running', 'completed', 'failed', 'interrupted')
    ),
    started_at timestamptz NOT NULL,
    completed_at timestamptz,
    outcome jsonb,
    CHECK (outcome IS NULL OR (
        outcome ? 'successful' AND outcome ? 'summary'
    ))
);

CREATE INDEX pack_runs_session_started_idx
    ON merlin_storage.pack_runs (session_id, started_at DESC);

CREATE TABLE merlin_storage.pack_turns (
    id uuid PRIMARY KEY,
    run_id uuid NOT NULL REFERENCES merlin_storage.pack_runs(id) ON DELETE CASCADE,
    sequence integer NOT NULL CHECK (sequence >= 0),
    role text NOT NULL CHECK (role IN ('attacker', 'target')),
    content text NOT NULL,
    created_at timestamptz NOT NULL,
    UNIQUE (run_id, sequence)
);

CREATE INDEX pack_turns_run_order_idx
    ON merlin_storage.pack_turns (run_id, sequence);
