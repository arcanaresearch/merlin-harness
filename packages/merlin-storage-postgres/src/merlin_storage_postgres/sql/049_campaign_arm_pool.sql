-- Durable bandit arm pool: one row per admitted (target x scenario x semantic x
-- strategy) arm. Arms enter in 'trial', may promote to 'active' once they show
-- signal, and retire once they exhaust their budget without signal. The natural
-- key keeps re-admission idempotent across waves and campaign restarts.
CREATE TABLE merlin_storage.campaign_arms (
    id text PRIMARY KEY,
    target_id text NOT NULL CHECK (target_id <> ''),
    scenario_fingerprint text NOT NULL CHECK (scenario_fingerprint <> ''),
    semantic_id text NOT NULL CHECK (semantic_id <> ''),
    strategy_id text NOT NULL CHECK (strategy_id <> ''),
    technique_id text,
    objective text NOT NULL CHECK (objective <> ''),
    source text NOT NULL CHECK (source IN ('seed', 'brainstorm')),
    novelty_digest text,
    status text NOT NULL DEFAULT 'trial' CHECK (status IN ('trial', 'active', 'retired')),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (target_id, scenario_fingerprint, semantic_id, strategy_id)
);

CREATE INDEX campaign_arms_target_scenario_status_idx
    ON merlin_storage.campaign_arms
    (target_id, scenario_fingerprint, status, created_at, id);
