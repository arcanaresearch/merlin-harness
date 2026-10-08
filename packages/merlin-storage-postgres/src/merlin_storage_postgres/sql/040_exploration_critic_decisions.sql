CREATE TABLE merlin_storage.exploration_critic_decisions (
    id text PRIMARY KEY,
    campaign_id text NOT NULL,
    hypothesis_id text NOT NULL,
    verdict text NOT NULL CHECK (verdict IN ('accept', 'revise', 'reject', 'abstain')),
    decision jsonb NOT NULL,
    decision_digest text NOT NULL,
    decided_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX exploration_critic_decisions_campaign_hypothesis_idx
    ON merlin_storage.exploration_critic_decisions
    (campaign_id, hypothesis_id, decided_at, id);

CREATE INDEX exploration_critic_decisions_campaign_verdict_idx
    ON merlin_storage.exploration_critic_decisions
    (campaign_id, verdict, decided_at, id);

CREATE FUNCTION merlin_storage.reject_exploration_critic_decision_mutation()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'exploration critic decisions are append-only';
END;
$$;

CREATE TRIGGER exploration_critic_decisions_append_only
BEFORE UPDATE OR DELETE ON merlin_storage.exploration_critic_decisions
FOR EACH ROW EXECUTE FUNCTION merlin_storage.reject_exploration_critic_decision_mutation();
