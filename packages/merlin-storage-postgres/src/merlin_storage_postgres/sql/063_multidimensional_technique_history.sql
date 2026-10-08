ALTER TABLE merlin_storage.technique_history
ADD COLUMN language_id text NOT NULL DEFAULT 'und'
CHECK (btrim(language_id) <> '');

ALTER TABLE merlin_storage.technique_history
ADD COLUMN mutation_id text NOT NULL DEFAULT 'none'
CHECK (btrim(mutation_id) <> '');

ALTER TABLE merlin_storage.technique_history
DROP CONSTRAINT technique_history_pkey;

ALTER TABLE merlin_storage.technique_history
ADD PRIMARY KEY (
    target_id,
    adapter_generation,
    technique_id,
    controller_id,
    payload_family,
    language_id,
    mutation_id
);
