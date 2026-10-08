ALTER TABLE merlin_storage.sessions
    ADD COLUMN parent_session_id uuid REFERENCES merlin_storage.sessions(id),
    ADD COLUMN forked_from_sequence bigint,
    ADD COLUMN projection_head bigint;

ALTER TABLE merlin_storage.messages
    ADD COLUMN source_message_id uuid REFERENCES merlin_storage.messages(id);

UPDATE merlin_storage.messages SET source_message_id = id WHERE source_message_id IS NULL;
ALTER TABLE merlin_storage.messages ALTER COLUMN source_message_id SET NOT NULL;

CREATE TABLE merlin_storage.conversation_compactions (
    id text PRIMARY KEY,
    session_id uuid NOT NULL REFERENCES merlin_storage.sessions(id) ON DELETE CASCADE,
    source_start bigint NOT NULL,
    source_end bigint NOT NULL,
    source_digest text NOT NULL CHECK (length(source_digest) = 64),
    summary text NOT NULL,
    summary_digest text NOT NULL CHECK (length(summary_digest) = 64),
    algorithm text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    CHECK (source_start <= source_end),
    UNIQUE (session_id, source_start, source_end, source_digest)
);

CREATE INDEX conversation_compactions_session_idx
    ON merlin_storage.conversation_compactions (session_id, source_end DESC);
