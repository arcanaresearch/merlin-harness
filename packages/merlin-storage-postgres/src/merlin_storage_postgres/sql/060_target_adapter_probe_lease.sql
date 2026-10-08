ALTER TABLE merlin_storage.target_adapter_circuits
    ADD COLUMN probe_token uuid;

UPDATE merlin_storage.target_adapter_circuits
SET state = 'open', retry_at = least(retry_at, now())
WHERE state = 'half_open';

ALTER TABLE merlin_storage.target_adapter_circuits
    ADD CONSTRAINT target_adapter_half_open_probe_token
    CHECK ((state = 'half_open') = (probe_token IS NOT NULL));
