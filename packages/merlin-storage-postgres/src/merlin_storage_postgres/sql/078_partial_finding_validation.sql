-- Graded partial compliance: findings may close as 'partial' with a signal,
-- observations may record a partial outcome, and technique attempts may
-- terminate in the 'partial' state (already terminal in the contracts).

ALTER TABLE merlin_storage.findings
    ADD COLUMN partial_signal jsonb;

DO $$
DECLARE
    r record;
BEGIN
    FOR r IN
        SELECT c.conname
        FROM pg_constraint c
        JOIN pg_class t ON t.oid = c.conrelid
        JOIN pg_namespace n ON n.oid = t.relnamespace
        WHERE n.nspname = 'merlin_storage' AND t.relname = 'findings'
          AND c.contype = 'c' AND pg_get_constraintdef(c.oid) LIKE '%unvalidated%'
    LOOP
        EXECUTE format('ALTER TABLE merlin_storage.findings DROP CONSTRAINT %I', r.conname);
    END LOOP;

    FOR r IN
        SELECT c.conname
        FROM pg_constraint c
        JOIN pg_class t ON t.oid = c.conrelid
        JOIN pg_namespace n ON n.oid = t.relnamespace
        WHERE n.nspname = 'merlin_storage' AND t.relname = 'campaign_observations'
          AND c.contype = 'c' AND pg_get_constraintdef(c.oid) LIKE '%not_observed%'
    LOOP
        EXECUTE format(
            'ALTER TABLE merlin_storage.campaign_observations DROP CONSTRAINT %I', r.conname
        );
    END LOOP;

    FOR r IN
        SELECT c.conname, pg_get_constraintdef(c.oid) AS definition
        FROM pg_constraint c
        JOIN pg_class t ON t.oid = c.conrelid
        JOIN pg_namespace n ON n.oid = t.relnamespace
        WHERE n.nspname = 'merlin_storage' AND t.relname = 'campaign_technique_attempts'
          AND c.contype = 'c'
          AND (
              pg_get_constraintdef(c.oid) LIKE '%effect_acknowledged%'
              OR pg_get_constraintdef(c.oid) LIKE '%terminal_at%'
          )
    LOOP
        EXECUTE format(
            'ALTER TABLE merlin_storage.campaign_technique_attempts DROP CONSTRAINT %I', r.conname
        );
    END LOOP;
END $$;

ALTER TABLE merlin_storage.findings
    ADD CONSTRAINT findings_validation_check
    CHECK (validation IN ('unvalidated', 'validated', 'refuted', 'inconclusive', 'partial'));

ALTER TABLE merlin_storage.campaign_observations
    ADD CONSTRAINT campaign_observations_outcome_check
    CHECK (outcome IN ('observed', 'not_observed', 'inconclusive', 'error', 'partial'));

ALTER TABLE merlin_storage.campaign_technique_attempts
    ADD CONSTRAINT campaign_technique_attempts_state_check
    CHECK (
        state IN ('planned', 'active', 'effect_acknowledged', 'observed', 'confirmed',
                  'refuted', 'inconclusive', 'partial', 'blocked', 'cancelled', 'ambiguous')
    );

ALTER TABLE merlin_storage.campaign_technique_attempts
    ADD CONSTRAINT campaign_technique_attempts_terminal_check
    CHECK (
        (state IN ('confirmed', 'refuted', 'inconclusive', 'partial',
                   'blocked', 'cancelled', 'ambiguous'))
        = (terminal_at IS NOT NULL AND coalesce(terminal_reason, '') <> '')
    );
