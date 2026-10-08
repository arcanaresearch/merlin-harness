ALTER TABLE merlin_storage.findings
    ADD COLUMN causal_ambiguity boolean NOT NULL DEFAULT false,
    ADD COLUMN impact_level smallint NOT NULL DEFAULT 0
        CHECK (impact_level BETWEEN 0 AND 4),
    ADD COLUMN requires_independent_reproduction boolean NOT NULL DEFAULT false;

COMMENT ON COLUMN merlin_storage.findings.causal_ambiguity IS
    'Whether attributable evidence leaves the finding causally ambiguous.';
COMMENT ON COLUMN merlin_storage.findings.impact_level IS
    'Typed impact tier from 0 (ordinary) through 4 (highest impact).';
COMMENT ON COLUMN merlin_storage.findings.requires_independent_reproduction IS
    'Whether policy explicitly requires reproduction in fresh equivalent target state.';
