-- Adaptive catalogue mutations are a first-class generated ARM family.
ALTER TABLE merlin_storage.campaign_arms DROP CONSTRAINT campaign_arms_source_check;

ALTER TABLE merlin_storage.campaign_arms
    ADD CONSTRAINT campaign_arms_source_check
    CHECK (source IN (
        'seed',
        'brainstorm',
        'axis-expansion',
        'gap-fill',
        'win-generalization',
        'mutation',
        'catalog-mutation',
        'crossover',
        'failure-derived',
        'boundary',
        'contextual'
    ));
