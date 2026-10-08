-- Four-axis arm taxonomy vocabulary: one row per (axis, slug) value that
-- parameterizes the register x framing x encoding x structure product space.
-- Values enter idempotently (PRIMARY KEY (axis, slug)); slugs are stable,
-- human-readable keys rather than UUID-style row ids so axis-expansion and
-- gap-fill proposals can be replayed safely across waves.
CREATE TABLE merlin_storage.campaign_axis_values (
    axis text NOT NULL CHECK (axis <> ''),
    slug text NOT NULL CHECK (slug <> ''),
    display_name text NOT NULL CHECK (display_name <> ''),
    instruction_fragment text NOT NULL CHECK (instruction_fragment <> ''),
    origin text NOT NULL CHECK (origin <> ''),
    obscurity integer NOT NULL DEFAULT 3 CHECK (obscurity BETWEEN 1 AND 5),
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (axis, slug)
);

-- Attach the four-axis coordinates of an arm so gap-fill can enumerate cells
-- the current pool does not yet cover. JSON of {register, framing, encoding,
-- structure}; members may be absent or null for single-shot / unencoded cells.
ALTER TABLE merlin_storage.campaign_arms ADD COLUMN axis_coords jsonb;
