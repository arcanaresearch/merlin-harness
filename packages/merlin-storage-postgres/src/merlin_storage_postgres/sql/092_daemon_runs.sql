CREATE TABLE merlin_storage.daemon_runs (
    id text PRIMARY KEY CHECK (id <> ''),
    pid integer NOT NULL CHECK (pid > 0),
    started_at timestamptz NOT NULL DEFAULT now(),
    heartbeat_at timestamptz NOT NULL DEFAULT now(),
    stop_requested_at timestamptz,
    stopped_at timestamptz,
    status text NOT NULL DEFAULT 'running'
        CHECK (status IN ('running', 'stopping', 'stopped', 'unclean')),
    stop_kind text CHECK (
        stop_kind IN ('clean', 'fatal_exception', 'signal', 'missing_heartbeat', 'unknown')
    ),
    stop_reason text,
    details jsonb NOT NULL DEFAULT '{}'::jsonb
        CHECK (jsonb_typeof(details) = 'object'),
    CHECK (
        (status = 'running'
            AND stop_requested_at IS NULL
            AND stopped_at IS NULL
            AND stop_kind IS NULL)
        OR
        (status = 'stopping'
            AND stop_requested_at IS NOT NULL
            AND stopped_at IS NULL
            AND stop_kind IS NOT NULL)
        OR
        (status IN ('stopped', 'unclean')
            AND stopped_at IS NOT NULL
            AND stop_kind IS NOT NULL)
    )
);

CREATE UNIQUE INDEX daemon_runs_one_active_idx
    ON merlin_storage.daemon_runs ((true))
    WHERE status IN ('running', 'stopping');

CREATE INDEX daemon_runs_started_idx
    ON merlin_storage.daemon_runs (started_at DESC);

COMMENT ON TABLE merlin_storage.daemon_runs IS
    'One durable row per Merlin daemon process incarnation.';

