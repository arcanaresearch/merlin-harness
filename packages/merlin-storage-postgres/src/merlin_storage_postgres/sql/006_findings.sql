CREATE TABLE merlin_storage.findings (
    id uuid PRIMARY KEY,
    session_id uuid NOT NULL REFERENCES merlin_storage.sessions(id) ON DELETE CASCADE,
    title text NOT NULL CHECK (title <> ''),
    severity text NOT NULL CHECK (
        severity IN ('informational', 'low', 'medium', 'high', 'critical')
    ),
    summary text NOT NULL CHECK (summary <> ''),
    reproduction text NOT NULL,
    remediation text NOT NULL,
    creator text NOT NULL CHECK (creator <> ''),
    status text NOT NULL DEFAULT 'draft' CHECK (status IN ('draft', 'accepted', 'rejected')),
    created_at timestamptz NOT NULL DEFAULT now(),
    dispositioned_at timestamptz
);

CREATE INDEX findings_session_created_idx
    ON merlin_storage.findings (session_id, created_at DESC, id DESC);

CREATE TABLE merlin_storage.finding_evidence (
    finding_id uuid NOT NULL REFERENCES merlin_storage.findings(id) ON DELETE CASCADE,
    ordinal integer NOT NULL CHECK (ordinal >= 0),
    reference text NOT NULL CHECK (reference <> ''),
    PRIMARY KEY (finding_id, ordinal)
);
