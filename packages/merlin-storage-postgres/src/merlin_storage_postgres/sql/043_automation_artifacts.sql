CREATE TABLE merlin_storage.automation_artifacts (
    digest text PRIMARY KEY CHECK (digest ~ '^sha256:[0-9a-f]{64}$'),
    request_id text NOT NULL CHECK (request_id <> ''),
    owning_attempt_id uuid NOT NULL REFERENCES merlin_storage.campaign_technique_attempts(id) ON DELETE RESTRICT,
    author_identity text NOT NULL CHECK (author_identity <> ''),
    author_model text NOT NULL CHECK (author_model <> ''),
    origin text NOT NULL CHECK (origin <> ''),
    schema_id text NOT NULL CHECK (schema_id <> ''),
    max_steps integer NOT NULL CHECK (max_steps > 0),
    payload jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (digest, owning_attempt_id, origin)
);

CREATE TABLE merlin_storage.automation_artifact_audits (
    artifact_digest text PRIMARY KEY REFERENCES merlin_storage.automation_artifacts(digest) ON DELETE RESTRICT,
    auditor_identity text NOT NULL CHECK (auditor_identity <> ''),
    auditor_model text NOT NULL CHECK (auditor_model <> ''),
    decision text NOT NULL CHECK (decision IN ('accept', 'reject', 'revise')),
    reasons text[] NOT NULL CHECK (cardinality(reasons) > 0),
    static_check_refs text[] NOT NULL,
    fixture_evidence_refs text[] NOT NULL,
    payload_digest text NOT NULL CHECK (payload_digest ~ '^sha256:[0-9a-f]{64}$'),
    created_at timestamptz NOT NULL DEFAULT now(),
    CHECK (decision <> 'accept' OR (cardinality(static_check_refs) > 0 AND cardinality(fixture_evidence_refs) > 0))
);

CREATE TABLE merlin_storage.automation_artifact_admissions (
    artifact_digest text PRIMARY KEY REFERENCES merlin_storage.automation_artifacts(digest) ON DELETE RESTRICT,
    owning_attempt_id uuid NOT NULL REFERENCES merlin_storage.campaign_technique_attempts(id) ON DELETE RESTRICT,
    origin text NOT NULL CHECK (origin <> ''),
    allowed_operations text[] NOT NULL CHECK (cardinality(allowed_operations) > 0),
    environment_digest text NOT NULL CHECK (environment_digest ~ '^sha256:[0-9a-f]{64}$'),
    payload_digest text NOT NULL CHECK (payload_digest ~ '^sha256:[0-9a-f]{64}$'),
    admitted_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (artifact_digest, owning_attempt_id),
    FOREIGN KEY (artifact_digest, owning_attempt_id, origin)
        REFERENCES merlin_storage.automation_artifacts(digest, owning_attempt_id, origin)
        ON DELETE RESTRICT
);

CREATE TABLE merlin_storage.automation_artifact_runs (
    run_id text PRIMARY KEY CHECK (run_id <> ''),
    artifact_digest text NOT NULL REFERENCES merlin_storage.automation_artifact_admissions(artifact_digest) ON DELETE RESTRICT,
    owning_attempt_id uuid NOT NULL REFERENCES merlin_storage.campaign_technique_attempts(id) ON DELETE RESTRICT,
    state text NOT NULL CHECK (state IN ('executing', 'completed', 'failed', 'ambiguous', 'cancelled')),
    events jsonb NOT NULL DEFAULT '[]',
    evidence_refs text[] NOT NULL DEFAULT '{}',
    error text,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    terminal_at timestamptz,
    CHECK ((state = 'executing') = (terminal_at IS NULL)),
    FOREIGN KEY (artifact_digest, owning_attempt_id)
        REFERENCES merlin_storage.automation_artifact_admissions(artifact_digest, owning_attempt_id)
        ON DELETE RESTRICT
);

CREATE INDEX automation_artifacts_attempt_idx
    ON merlin_storage.automation_artifacts (owning_attempt_id, created_at);
CREATE INDEX automation_artifact_runs_attempt_idx
    ON merlin_storage.automation_artifact_runs (owning_attempt_id, created_at);

COMMENT ON TABLE merlin_storage.automation_artifacts IS 'Immutable declarative automation authored offline for one technique attempt.';
COMMENT ON COLUMN merlin_storage.automation_artifacts.digest IS 'Canonical SHA-256 identity of the immutable artifact content.';
COMMENT ON COLUMN merlin_storage.automation_artifacts.request_id IS 'Typed Campaign Lead automation-request identity.';
COMMENT ON COLUMN merlin_storage.automation_artifacts.owning_attempt_id IS 'Technique attempt this artifact may implement; it cannot be rebound.';
COMMENT ON COLUMN merlin_storage.automation_artifacts.author_identity IS 'Offline author-agent identity used for separation of duties.';
COMMENT ON COLUMN merlin_storage.automation_artifacts.author_model IS 'Model route that authored the artifact.';
COMMENT ON COLUMN merlin_storage.automation_artifacts.origin IS 'Reviewed target origin the declarative flow is constrained to.';
COMMENT ON COLUMN merlin_storage.automation_artifacts.schema_id IS 'Declarative artifact schema identifier.';
COMMENT ON COLUMN merlin_storage.automation_artifacts.max_steps IS 'Sealed upper bound on artifact operations.';
COMMENT ON COLUMN merlin_storage.automation_artifacts.payload IS 'Canonical artifact payload including ordered operations and captures.';
COMMENT ON COLUMN merlin_storage.automation_artifacts.created_at IS 'Time immutable artifact content was first persisted.';

COMMENT ON TABLE merlin_storage.automation_artifact_audits IS 'Independent digest-bound audit decisions with static and fixture evidence.';
COMMENT ON COLUMN merlin_storage.automation_artifact_audits.artifact_digest IS 'Exact immutable artifact reviewed by the auditor.';
COMMENT ON COLUMN merlin_storage.automation_artifact_audits.auditor_identity IS 'Independent auditor-agent identity.';
COMMENT ON COLUMN merlin_storage.automation_artifact_audits.auditor_model IS 'Model route that produced the audit decision.';
COMMENT ON COLUMN merlin_storage.automation_artifact_audits.decision IS 'Accept, reject, or revise disposition; the auditor never edits inline.';
COMMENT ON COLUMN merlin_storage.automation_artifact_audits.reasons IS 'Attributable audit reasons.';
COMMENT ON COLUMN merlin_storage.automation_artifact_audits.static_check_refs IS 'References to deterministic static-check receipts.';
COMMENT ON COLUMN merlin_storage.automation_artifact_audits.fixture_evidence_refs IS 'References to dry-fixture evaluation evidence.';
COMMENT ON COLUMN merlin_storage.automation_artifact_audits.payload_digest IS 'Canonical digest preventing audit replay with changed content.';
COMMENT ON COLUMN merlin_storage.automation_artifact_audits.created_at IS 'Time the independent audit was persisted.';

COMMENT ON TABLE merlin_storage.automation_artifact_admissions IS 'Daemon admissions sealing artifact authority, environment, origin, and owner.';
COMMENT ON COLUMN merlin_storage.automation_artifact_admissions.artifact_digest IS 'Exact independently accepted artifact admitted for execution.';
COMMENT ON COLUMN merlin_storage.automation_artifact_admissions.owning_attempt_id IS 'Technique attempt that owns every artifact run and resulting evidence.';
COMMENT ON COLUMN merlin_storage.automation_artifact_admissions.origin IS 'Reviewed target origin preserved from the artifact.';
COMMENT ON COLUMN merlin_storage.automation_artifact_admissions.allowed_operations IS 'Finite operation set the deterministic executor may dispatch.';
COMMENT ON COLUMN merlin_storage.automation_artifact_admissions.environment_digest IS 'Digest of the reviewed execution environment and tool schemas.';
COMMENT ON COLUMN merlin_storage.automation_artifact_admissions.payload_digest IS 'Canonical digest preventing admission replay with changed authority.';
COMMENT ON COLUMN merlin_storage.automation_artifact_admissions.admitted_at IS 'Time the daemon admitted the immutable artifact.';

COMMENT ON TABLE merlin_storage.automation_artifact_runs IS 'Deterministic artifact executions and normalized evidence lineage.';
COMMENT ON COLUMN merlin_storage.automation_artifact_runs.run_id IS 'Stable subordinate run identity.';
COMMENT ON COLUMN merlin_storage.automation_artifact_runs.artifact_digest IS 'Exact admitted artifact executed by this run.';
COMMENT ON COLUMN merlin_storage.automation_artifact_runs.owning_attempt_id IS 'Technique attempt receiving all run effects and evidence.';
COMMENT ON COLUMN merlin_storage.automation_artifact_runs.state IS 'Monotonic executing or terminal run lifecycle state.';
COMMENT ON COLUMN merlin_storage.automation_artifact_runs.events IS 'Normalized per-operation results without model-generated interpretation.';
COMMENT ON COLUMN merlin_storage.automation_artifact_runs.evidence_refs IS 'Content-addressed evidence references emitted by artifact operations.';
COMMENT ON COLUMN merlin_storage.automation_artifact_runs.error IS 'Typed terminal failure detail when execution did not complete.';
COMMENT ON COLUMN merlin_storage.automation_artifact_runs.created_at IS 'Time deterministic execution began.';
COMMENT ON COLUMN merlin_storage.automation_artifact_runs.updated_at IS 'Time the latest lifecycle state was persisted.';
COMMENT ON COLUMN merlin_storage.automation_artifact_runs.terminal_at IS 'Time the run reached an immutable terminal state.';
