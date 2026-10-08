ALTER TABLE merlin_storage.campaign_budget_reservations
    ADD COLUMN reserved_cost_usd numeric NOT NULL DEFAULT 0
        CHECK (reserved_cost_usd >= 0),
    ADD COLUMN settled_cost_usd numeric NULL
        CHECK (settled_cost_usd >= 0);
