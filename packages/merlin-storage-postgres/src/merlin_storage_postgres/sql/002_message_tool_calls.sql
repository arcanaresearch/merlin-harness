ALTER TABLE merlin_storage.messages
    ADD COLUMN tool_calls jsonb NOT NULL DEFAULT '[]'::jsonb;
