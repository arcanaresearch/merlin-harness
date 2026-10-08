CREATE SCHEMA IF NOT EXISTS merlin_storage;

CREATE TABLE merlin_storage.sessions (
    id uuid PRIMARY KEY,
    title text,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE merlin_storage.messages (
    id uuid PRIMARY KEY,
    sequence bigint GENERATED ALWAYS AS IDENTITY UNIQUE,
    session_id uuid NOT NULL REFERENCES merlin_storage.sessions(id) ON DELETE CASCADE,
    role text NOT NULL CHECK (role <> ''),
    content text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    name text,
    tool_call_id text
);

CREATE INDEX messages_session_order_idx
    ON merlin_storage.messages (session_id, sequence);

CREATE TABLE merlin_storage.events (
    id uuid UNIQUE NOT NULL,
    sequence bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    session_id uuid NOT NULL REFERENCES merlin_storage.sessions(id) ON DELETE CASCADE,
    type text NOT NULL CHECK (type <> ''),
    payload jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX events_session_order_idx
    ON merlin_storage.events (session_id, sequence);
