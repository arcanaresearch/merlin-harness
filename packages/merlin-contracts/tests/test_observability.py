import pytest
from merlin_contracts import (
    ArmIdentity,
    ArmOutcome,
    CampaignCounterEvent,
    CampaignCounters,
    HypothesisRecord,
)
from pydantic import ValidationError


def test_hypothesis_requires_immutable_parent_lineage_and_creating_evidence() -> None:
    child = HypothesisRecord(
        id="hypothesis/child",
        campaign_id="campaign-1",
        target_id="target-1",
        scenario_fingerprint="scenario-1",
        claim="A gradual supervisor claim may cross the refusal boundary.",
        parent_hypothesis_id="hypothesis/root",
        creating_evidence_refs=("evidence:near-miss",),
        lineage=("hypothesis/root", "hypothesis/child"),
    )

    assert child.lineage[-1] == child.id
    with pytest.raises(ValidationError, match="immediate lineage predecessor"):
        child.model_copy(update={"parent_hypothesis_id": "hypothesis/other"}).model_validate(
            child.model_copy(update={"parent_hypothesis_id": "hypothesis/other"})
        )


def test_counter_event_separates_work_and_types_infrastructure_failure() -> None:
    event = CampaignCounterEvent(
        id="counter/1",
        campaign_id="campaign-1",
        counters=CampaignCounters(target_calls=2, model_steps=3, validation_calls=1),
    )
    assert event.counters.target_calls == 2
    assert event.counters.model_steps == 3
    assert event.counters.reproduction_calls == 0

    with pytest.raises(ValidationError, match="failure category"):
        CampaignCounterEvent(
            id="counter/infra",
            campaign_id="campaign-1",
            counters=CampaignCounters(infrastructure_failures=1),
        )


def test_arm_outcome_carries_full_identity_and_language_evidence() -> None:
    outcome = ArmOutcome(
        id="outcome/1",
        campaign_id="campaign-1",
        arm_id="arm/1",
        technique_attempt_id="attempt/1",
        identity=ArmIdentity(
            target_id="target-1",
            scenario_fingerprint="scenario-1",
            surface_id="chat",
            technique_id="prompt-override",
            controller_id="crescendo",
            mutation_id="authority-framing",
            actual_language="ms",
            state_id="fresh-session",
        ),
        requested_language="ms",
        actual_language="ms",
        language_evidence_status="observed",
        language_evidence_source="target response detector",
        outcome="clean_negative",
        counters=CampaignCounters(target_calls=1, model_steps=2),
        evidence_refs=("exchange:1",),
        reason="Target retained its refusal boundary.",
    )

    assert outcome.identity.surface_id == "chat"
    assert outcome.actual_language == "ms"
