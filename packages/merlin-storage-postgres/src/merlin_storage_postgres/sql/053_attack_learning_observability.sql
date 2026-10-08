-- Pre-launch breaking schema: campaign arms now carry the complete schedulable
-- identity and an inspectable ranking decision. Existing databases containing
-- partial campaign-arm rows discard only that regenerable learning pool rather
-- than having missing identity invented. No pre-053 table has an inbound FK.
DELETE FROM merlin_storage.campaign_arms;

ALTER TABLE merlin_storage.campaign_arms
    DROP CONSTRAINT campaign_arms_target_id_scenario_fingerprint_semantic_id_st_key,
    ADD COLUMN surface_id text NOT NULL CHECK (surface_id <> ''),
    ADD COLUMN controller_id text NOT NULL CHECK (controller_id <> ''),
    ADD COLUMN mutation_id text NOT NULL CHECK (mutation_id <> ''),
    ADD COLUMN actual_language text NOT NULL CHECK (actual_language <> ''),
    ADD COLUMN state_id text NOT NULL CHECK (state_id <> ''),
    ADD COLUMN rank_components jsonb NOT NULL CHECK (jsonb_typeof(rank_components) = 'object'),
    ADD COLUMN eligibility_reasons text[] NOT NULL CHECK (cardinality(eligibility_reasons) > 0),
    ADD COLUMN decision_reason text NOT NULL CHECK (decision_reason <> ''),
    ADD CONSTRAINT campaign_arms_complete_identity_key UNIQUE
        (target_id, scenario_fingerprint, surface_id, semantic_id, technique_id,
         controller_id, mutation_id, actual_language, state_id),
    ADD CONSTRAINT campaign_arms_identity_reference_key UNIQUE
        (id, target_id, scenario_fingerprint, surface_id, technique_id, controller_id,
         mutation_id, actual_language, state_id);

CREATE TABLE merlin_storage.campaign_hypotheses (
    id text PRIMARY KEY CHECK (id <> ''),
    schema_version smallint NOT NULL DEFAULT 1 CHECK (schema_version = 1),
    campaign_id text NOT NULL REFERENCES merlin_storage.assessment_campaigns(id) ON DELETE RESTRICT,
    target_id text NOT NULL CHECK (target_id <> ''),
    scenario_fingerprint text NOT NULL CHECK (scenario_fingerprint <> ''),
    claim text NOT NULL CHECK (claim <> ''),
    parent_hypothesis_id text,
    creating_evidence_refs text[] NOT NULL CHECK (cardinality(creating_evidence_refs) > 0),
    lineage text[] NOT NULL CHECK (cardinality(lineage) > 0),
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (campaign_id, id),
    FOREIGN KEY (campaign_id, parent_hypothesis_id)
        REFERENCES merlin_storage.campaign_hypotheses(campaign_id, id) ON DELETE RESTRICT,
    CHECK (lineage[array_length(lineage, 1)] = id),
    CHECK (parent_hypothesis_id IS NULL OR parent_hypothesis_id <> id)
);

CREATE FUNCTION merlin_storage.reject_hypothesis_rewrite() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'campaign hypotheses are immutable; append a lifecycle event';
END;
$$;

CREATE TRIGGER campaign_hypotheses_immutable
    BEFORE UPDATE OR DELETE ON merlin_storage.campaign_hypotheses
    FOR EACH ROW EXECUTE FUNCTION merlin_storage.reject_hypothesis_rewrite();

CREATE FUNCTION merlin_storage.validate_hypothesis_lineage() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    parent_lineage text[];
    parent_target_id text;
    parent_scenario_fingerprint text;
BEGIN
    IF NEW.parent_hypothesis_id IS NULL THEN
        IF NEW.lineage <> ARRAY[NEW.id] THEN
            RAISE EXCEPTION 'root hypothesis lineage must contain only itself';
        END IF;
        RETURN NEW;
    END IF;
    SELECT lineage, target_id, scenario_fingerprint
      INTO parent_lineage, parent_target_id, parent_scenario_fingerprint
      FROM merlin_storage.campaign_hypotheses
     WHERE campaign_id = NEW.campaign_id AND id = NEW.parent_hypothesis_id;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'parent hypothesis does not exist in campaign';
    END IF;
    IF NEW.target_id <> parent_target_id
       OR NEW.scenario_fingerprint <> parent_scenario_fingerprint THEN
        RAISE EXCEPTION 'child hypothesis scope must match its parent';
    END IF;
    IF NEW.lineage <> parent_lineage || NEW.id THEN
        RAISE EXCEPTION 'child hypothesis lineage must extend its parent';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER campaign_hypotheses_validate_lineage
    BEFORE INSERT ON merlin_storage.campaign_hypotheses
    FOR EACH ROW EXECUTE FUNCTION merlin_storage.validate_hypothesis_lineage();

CREATE TABLE merlin_storage.campaign_hypothesis_events (
    id text PRIMARY KEY CHECK (id <> ''),
    sequence bigint GENERATED ALWAYS AS IDENTITY UNIQUE,
    hypothesis_id text NOT NULL REFERENCES merlin_storage.campaign_hypotheses(id) ON DELETE RESTRICT,
    state text NOT NULL CHECK (
        state IN ('untested', 'in_progress', 'supported', 'falsified',
                  'blocked_by_infrastructure', 'superseded')
    ),
    evidence_refs text[] NOT NULL DEFAULT '{}',
    reason text,
    infrastructure_failure_category text CHECK (
        infrastructure_failure_category IN (
            'adapter_timeout', 'missing_response_pointer', 'delivery_failure',
            'unhealthy_target_session'
        )
    ),
    created_at timestamptz NOT NULL DEFAULT now(),
    CHECK (
        (state = 'blocked_by_infrastructure')
        = (infrastructure_failure_category IS NOT NULL)
    )
);

CREATE INDEX campaign_hypothesis_events_latest_idx
    ON merlin_storage.campaign_hypothesis_events (hypothesis_id, sequence DESC);

CREATE FUNCTION merlin_storage.reject_observability_rewrite() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION '% is append-only', TG_TABLE_NAME;
END;
$$;

CREATE TRIGGER campaign_hypothesis_events_append_only
    BEFORE UPDATE OR DELETE ON merlin_storage.campaign_hypothesis_events
    FOR EACH ROW EXECUTE FUNCTION merlin_storage.reject_observability_rewrite();

ALTER TABLE merlin_storage.campaign_technique_attempts
    ADD CONSTRAINT campaign_technique_attempt_identity_campaign_key UNIQUE (id, campaign_id);

CREATE TABLE merlin_storage.campaign_counter_events (
    id text PRIMARY KEY CHECK (id <> ''),
    campaign_id text NOT NULL REFERENCES merlin_storage.assessment_campaigns(id) ON DELETE RESTRICT,
    arm_id text REFERENCES merlin_storage.campaign_arms(id) ON DELETE RESTRICT,
    hypothesis_id text,
    technique_attempt_id uuid,
    target_calls integer NOT NULL CHECK (target_calls >= 0),
    model_steps integer NOT NULL CHECK (model_steps >= 0),
    validation_calls integer NOT NULL CHECK (validation_calls >= 0),
    reproduction_calls integer NOT NULL CHECK (reproduction_calls >= 0),
    infrastructure_failures integer NOT NULL CHECK (infrastructure_failures >= 0),
    infrastructure_failure_category text CHECK (
        infrastructure_failure_category IN (
            'adapter_timeout', 'missing_response_pointer', 'delivery_failure',
            'unhealthy_target_session'
        )
    ),
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (id, campaign_id),
    FOREIGN KEY (campaign_id, hypothesis_id)
        REFERENCES merlin_storage.campaign_hypotheses(campaign_id, id) ON DELETE RESTRICT,
    FOREIGN KEY (technique_attempt_id, campaign_id)
        REFERENCES merlin_storage.campaign_technique_attempts(id, campaign_id) ON DELETE RESTRICT,
    CHECK (
        target_calls + model_steps + validation_calls + reproduction_calls
        + infrastructure_failures > 0
    ),
    CHECK (
        (infrastructure_failures > 0) = (infrastructure_failure_category IS NOT NULL)
    )
);

CREATE INDEX campaign_counter_events_campaign_idx
    ON merlin_storage.campaign_counter_events (campaign_id, created_at, id);

CREATE TRIGGER campaign_counter_events_append_only
    BEFORE DELETE ON merlin_storage.campaign_counter_events
    FOR EACH ROW EXECUTE FUNCTION merlin_storage.reject_observability_rewrite();

CREATE TABLE merlin_storage.technique_language_evidence (
    technique_attempt_id uuid PRIMARY KEY,
    campaign_id text NOT NULL,
    requested_language text NOT NULL CHECK (requested_language <> ''),
    actual_language text NOT NULL CHECK (actual_language <> ''),
    language_evidence_status text NOT NULL CHECK (
        language_evidence_status IN ('observed', 'inferred', 'unavailable', 'inconclusive')
    ),
    language_evidence_source text NOT NULL CHECK (language_evidence_source <> ''),
    evidence_refs text[] NOT NULL CHECK (cardinality(evidence_refs) > 0),
    created_at timestamptz NOT NULL DEFAULT now(),
    FOREIGN KEY (technique_attempt_id, campaign_id)
        REFERENCES merlin_storage.campaign_technique_attempts(id, campaign_id) ON DELETE RESTRICT
);

CREATE INDEX technique_language_evidence_campaign_idx
    ON merlin_storage.technique_language_evidence (campaign_id, created_at, technique_attempt_id);

CREATE TRIGGER technique_language_evidence_append_only
    BEFORE UPDATE OR DELETE ON merlin_storage.technique_language_evidence
    FOR EACH ROW EXECUTE FUNCTION merlin_storage.reject_observability_rewrite();

CREATE TABLE merlin_storage.campaign_arm_outcomes (
    id text PRIMARY KEY CHECK (id <> ''),
    schema_version smallint NOT NULL DEFAULT 1 CHECK (schema_version = 1),
    campaign_id text NOT NULL REFERENCES merlin_storage.assessment_campaigns(id) ON DELETE RESTRICT,
    arm_id text NOT NULL,
    technique_attempt_id uuid NOT NULL,
    hypothesis_id text,
    target_id text NOT NULL,
    scenario_fingerprint text NOT NULL,
    surface_id text NOT NULL,
    technique_id text NOT NULL,
    controller_id text NOT NULL,
    mutation_id text NOT NULL,
    actual_language text NOT NULL,
    state_id text NOT NULL,
    requested_language text NOT NULL CHECK (requested_language <> ''),
    language_evidence_status text NOT NULL CHECK (
        language_evidence_status IN ('observed', 'inferred', 'unavailable', 'inconclusive')
    ),
    language_evidence_source text NOT NULL CHECK (language_evidence_source <> ''),
    outcome text NOT NULL CHECK (
        outcome IN ('authoritative_success', 'validated_finding', 'partial_signal',
                    'clean_negative', 'infrastructure_failure', 'ambiguous')
    ),
    counter_event_id text NOT NULL UNIQUE,
    evidence_refs text[] NOT NULL CHECK (cardinality(evidence_refs) > 0),
    signal double precision,
    distance_to_success double precision CHECK (distance_to_success >= 0),
    latency_ms integer NOT NULL CHECK (latency_ms >= 0),
    model_cost_usd double precision NOT NULL CHECK (model_cost_usd >= 0),
    reason text NOT NULL CHECK (reason <> ''),
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (campaign_id, arm_id, technique_attempt_id),
    FOREIGN KEY (
        arm_id, target_id, scenario_fingerprint, surface_id, technique_id, controller_id,
        mutation_id, actual_language, state_id
    ) REFERENCES merlin_storage.campaign_arms (
        id, target_id, scenario_fingerprint, surface_id, technique_id, controller_id,
        mutation_id, actual_language, state_id
    ) ON DELETE RESTRICT,
    FOREIGN KEY (technique_attempt_id, campaign_id)
        REFERENCES merlin_storage.campaign_technique_attempts(id, campaign_id) ON DELETE RESTRICT,
    FOREIGN KEY (campaign_id, hypothesis_id)
        REFERENCES merlin_storage.campaign_hypotheses(campaign_id, id) ON DELETE RESTRICT,
    FOREIGN KEY (counter_event_id, campaign_id)
        REFERENCES merlin_storage.campaign_counter_events(id, campaign_id) ON DELETE RESTRICT
);

CREATE INDEX campaign_arm_outcomes_comparable_idx
    ON merlin_storage.campaign_arm_outcomes
       (target_id, scenario_fingerprint, surface_id, technique_id, controller_id,
        mutation_id, actual_language, state_id, created_at DESC);

CREATE TRIGGER campaign_arm_outcomes_append_only
    BEFORE DELETE ON merlin_storage.campaign_arm_outcomes
    FOR EACH ROW EXECUTE FUNCTION merlin_storage.reject_observability_rewrite();

COMMENT ON TABLE merlin_storage.campaign_hypotheses IS
    'Immutable target-specific hypothesis identity; lifecycle changes are append-only events.';
COMMENT ON TABLE merlin_storage.campaign_counter_events IS
    'Idempotent attributable deltas for target calls, model steps, validation, reproduction, and infrastructure failures.';
COMMENT ON TABLE merlin_storage.technique_language_evidence IS
    'Append-only requested-versus-actual language evidence, written once per technique attempt.';
COMMENT ON TABLE merlin_storage.campaign_arm_outcomes IS
    'Immutable full-identity ARM results, including signal, cost, evidence, and typed infrastructure failure attribution.';
