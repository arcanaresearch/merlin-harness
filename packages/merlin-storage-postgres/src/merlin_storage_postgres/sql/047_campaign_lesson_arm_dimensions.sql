-- Persist the bandit arm dimensions on the durable learning ledger so
-- cross-campaign UCB ranking, novelty dedup, and signal-based retirement
-- operate on (semantic_id x strategy_id) arms instead of raw technique ids.
ALTER TABLE merlin_storage.campaign_lessons
    ADD COLUMN semantic_id text,
    ADD COLUMN strategy_id text NOT NULL DEFAULT 'direct';
