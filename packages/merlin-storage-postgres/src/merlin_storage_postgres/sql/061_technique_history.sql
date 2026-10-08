CREATE TABLE merlin_storage.technique_history (
    target_id text NOT NULL CHECK (btrim(target_id) <> ''),
    adapter_generation text NOT NULL CHECK (btrim(adapter_generation) <> ''),
    technique_id text NOT NULL CHECK (btrim(technique_id) <> ''),
    controller_id text NOT NULL CHECK (btrim(controller_id) <> ''),
    payload_family text NOT NULL CHECK (btrim(payload_family) <> ''),
    outcome text NOT NULL CHECK (outcome IN ('confirmed', 'refuted', 'blocked', 'unclear')),
    source_campaign_id text NOT NULL CHECK (btrim(source_campaign_id) <> ''),
    recorded_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (target_id, adapter_generation, technique_id, controller_id, payload_family)
);
