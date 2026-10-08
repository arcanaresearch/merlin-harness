ALTER TABLE merlin_storage.technique_history
DROP CONSTRAINT technique_history_outcome_check;

ALTER TABLE merlin_storage.technique_history
ADD CONSTRAINT technique_history_outcome_check
CHECK (outcome IN ('confirmed', 'refuted', 'blocked', 'blocked_terminal', 'partial', 'unclear'));

ALTER TABLE merlin_storage.technique_history
ADD COLUMN partial_signal jsonb;
