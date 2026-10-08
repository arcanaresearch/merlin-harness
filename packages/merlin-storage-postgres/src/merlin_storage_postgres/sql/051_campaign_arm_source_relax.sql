-- Generative arm discovery admits arms from the committed generator families in
-- addition to operator-seeded catalogue arms. 049 pinned the source column to
-- the seed/brainstorm pair; widen it to the full ``CampaignArmStore._SOURCE``
-- set so failure-derived, win-generalization, and the remaining committed
-- generators can persist without re-keying the admission boundary.
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
        'crossover',
        'failure-derived',
        'boundary',
        'contextual'
    ));
