CREATE TABLE merlin_storage.campaign_budget_reservations (
    campaign_id text NOT NULL REFERENCES merlin_storage.assessment_campaigns(id) ON DELETE CASCADE,
    reservation_id text NOT NULL,
    actor_kind text NOT NULL CHECK (actor_kind IN ('lead', 'worker')),
    reserved_tokens bigint NOT NULL CHECK (reserved_tokens >= 0),
    reserved_turns bigint NOT NULL CHECK (reserved_turns >= 0),
    settled_tokens bigint NULL CHECK (settled_tokens >= 0),
    settled_turns bigint NULL CHECK (settled_turns >= 0),
    state text NOT NULL CHECK (state IN ('active', 'settled', 'released', 'rejected')),
    created_at timestamptz NOT NULL DEFAULT now(),
    settled_at timestamptz NULL,
    PRIMARY KEY (campaign_id, reservation_id)
);

CREATE INDEX campaign_budget_reservations_active_idx
    ON merlin_storage.campaign_budget_reservations (campaign_id, state);
