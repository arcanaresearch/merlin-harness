CREATE TABLE merlin_storage.promotion_candidates (
    id text PRIMARY KEY,
    kind text NOT NULL CHECK (kind IN ('skill', 'guidance', 'lesson', 'routing', 'profile')),
    version text NOT NULL,
    baseline_version text NOT NULL,
    baseline_digest text NOT NULL,
    content jsonb NOT NULL,
    content_digest text NOT NULL,
    proposed_by text NOT NULL,
    profile_id text,
    profile_operation text,
    graph_local_composition boolean NOT NULL DEFAULT false,
    proposed_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (kind, version),
    UNIQUE (content_digest, kind)
);

CREATE TABLE merlin_storage.promotion_evaluations (
    id text PRIMARY KEY,
    candidate_id text NOT NULL REFERENCES merlin_storage.promotion_candidates(id),
    record jsonb NOT NULL,
    passed boolean NOT NULL,
    recorded_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE merlin_storage.promotion_decisions (
    id bigserial PRIMARY KEY,
    candidate_id text NOT NULL REFERENCES merlin_storage.promotion_candidates(id),
    operator_id text NOT NULL,
    decision text NOT NULL CHECK (decision IN ('accepted', 'rejected')),
    reason text NOT NULL,
    decided_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (candidate_id)
);

CREATE TABLE merlin_storage.promotion_activations (
    id bigserial PRIMARY KEY,
    kind text NOT NULL,
    candidate_id text NOT NULL REFERENCES merlin_storage.promotion_candidates(id),
    previous_candidate_id text REFERENCES merlin_storage.promotion_candidates(id),
    action text NOT NULL CHECK (action IN ('activated', 'rolled_back')),
    operator_id text NOT NULL,
    activated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE merlin_storage.active_promotions (
    kind text PRIMARY KEY,
    candidate_id text NOT NULL REFERENCES merlin_storage.promotion_candidates(id),
    activation_id bigint NOT NULL REFERENCES merlin_storage.promotion_activations(id)
);

CREATE FUNCTION merlin_storage.reject_promotion_history_mutation() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'promotion history is immutable';
END;
$$;

CREATE TRIGGER promotion_candidates_immutable BEFORE UPDATE OR DELETE
ON merlin_storage.promotion_candidates FOR EACH ROW
EXECUTE FUNCTION merlin_storage.reject_promotion_history_mutation();
CREATE TRIGGER promotion_evaluations_immutable BEFORE UPDATE OR DELETE
ON merlin_storage.promotion_evaluations FOR EACH ROW
EXECUTE FUNCTION merlin_storage.reject_promotion_history_mutation();
CREATE TRIGGER promotion_decisions_immutable BEFORE UPDATE OR DELETE
ON merlin_storage.promotion_decisions FOR EACH ROW
EXECUTE FUNCTION merlin_storage.reject_promotion_history_mutation();
CREATE TRIGGER promotion_activations_immutable BEFORE UPDATE OR DELETE
ON merlin_storage.promotion_activations FOR EACH ROW
EXECUTE FUNCTION merlin_storage.reject_promotion_history_mutation();
