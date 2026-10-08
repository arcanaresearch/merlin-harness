CREATE TABLE merlin_storage.target_adapter_circuits (
    scope_key text PRIMARY KEY CHECK (btrim(scope_key) <> ''),
    state text NOT NULL CHECK (state IN ('open', 'half_open')),
    failure_count integer NOT NULL CHECK (failure_count > 0),
    retry_at timestamptz NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now()
);
