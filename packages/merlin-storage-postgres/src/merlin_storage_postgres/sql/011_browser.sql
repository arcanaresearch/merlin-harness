CREATE TABLE merlin_storage.browser_contexts (
    context_id text PRIMARY KEY,
    session_id uuid NOT NULL REFERENCES merlin_storage.sessions(id) ON DELETE CASCADE,
    target_id text NOT NULL,
    mode text NOT NULL CHECK (mode IN ('isolated', 'attached')),
    worker_id text NOT NULL,
    technique_id text NOT NULL,
    state text NOT NULL CHECK (state IN ('active', 'cleaned', 'restart_required')),
    allowed_origins jsonb NOT NULL,
    metadata jsonb NOT NULL,
    created_at timestamptz NOT NULL,
    updated_at timestamptz NOT NULL
);

CREATE TABLE merlin_storage.browser_tabs (
    context_id text NOT NULL REFERENCES merlin_storage.browser_contexts(context_id) ON DELETE CASCADE,
    tab_ref text NOT NULL,
    url text NOT NULL,
    opener_ref text,
    state text NOT NULL CHECK (state IN ('open', 'closed', 'blocked')),
    PRIMARY KEY (context_id, tab_ref)
);

CREATE TABLE merlin_storage.browser_actions (
    action_id uuid PRIMARY KEY,
    context_id text NOT NULL REFERENCES merlin_storage.browser_contexts(context_id) ON DELETE CASCADE,
    tab_ref text NOT NULL,
    step integer NOT NULL CHECK (step >= 0),
    kind text NOT NULL,
    document jsonb NOT NULL,
    created_at timestamptz NOT NULL
);
CREATE INDEX browser_actions_context_step_idx
    ON merlin_storage.browser_actions (context_id, step, created_at);

CREATE TABLE merlin_storage.browser_observations (
    observation_id uuid PRIMARY KEY,
    context_id text NOT NULL REFERENCES merlin_storage.browser_contexts(context_id) ON DELETE CASCADE,
    tab_ref text NOT NULL,
    frame_ref text,
    step integer NOT NULL CHECK (step >= 0),
    kind text NOT NULL,
    document jsonb NOT NULL,
    observed_at timestamptz NOT NULL
);
CREATE INDEX browser_observations_context_step_idx
    ON merlin_storage.browser_observations (context_id, step, observed_at);

CREATE TABLE merlin_storage.browser_evidence (
    evidence_ref text PRIMARY KEY,
    context_id text NOT NULL REFERENCES merlin_storage.browser_contexts(context_id) ON DELETE CASCADE,
    kind text NOT NULL,
    payload jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE merlin_storage.browser_observation_evidence (
    observation_id uuid NOT NULL REFERENCES merlin_storage.browser_observations(observation_id)
        ON DELETE CASCADE,
    evidence_ref text NOT NULL,
    ordinal integer NOT NULL CHECK (ordinal >= 0),
    PRIMARY KEY (observation_id, ordinal)
);

CREATE TABLE merlin_storage.browser_recon_handoffs (
    handoff_id text PRIMARY KEY,
    context_id text NOT NULL REFERENCES merlin_storage.browser_contexts(context_id) ON DELETE CASCADE,
    target_id text NOT NULL,
    method text NOT NULL,
    url text NOT NULL,
    request_evidence_ref text NOT NULL,
    response_evidence_ref text NOT NULL,
    document jsonb NOT NULL,
    created_at timestamptz NOT NULL
);
