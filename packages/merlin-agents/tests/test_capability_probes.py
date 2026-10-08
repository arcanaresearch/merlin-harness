import pytest
from merlin_agents.capability_probes import (
    CapabilityProbeKind,
    CapabilitySupport,
    ProbeObservation,
    ProbeOutcome,
    classify_probe_result,
    observed_capabilities,
    observed_surfaces,
    plan_capability_probes,
)


def test_plan_covers_harmless_capability_discovery_and_keeps_unknown_schedulable() -> None:
    plan = plan_capability_probes(
        advertised_languages=("en",), candidate_languages=("en", "ms", "JA")
    )

    assert {probe.kind for probe in plan.schedulable} == set(CapabilityProbeKind)
    assert all(
        "benign" in probe.instruction or "unique nonce" in probe.instruction
        for probe in plan.probes
    )
    assert all(probe.support is CapabilitySupport.UNKNOWN for probe in plan.probes[:6])
    language_probes = [
        probe for probe in plan.probes if probe.kind is CapabilityProbeKind.NON_ADVERTISED_LANGUAGE
    ]
    assert [probe.language for probe in language_probes] == ["ms", "ja"]
    assert all(probe.support is CapabilitySupport.NOT_ADVERTISED for probe in language_probes)


@pytest.mark.parametrize(
    ("support", "observation", "expected"),
    (
        (
            CapabilitySupport.UNKNOWN,
            ProbeObservation(True, True, False, "nonce returned", ("event-1",)),
            ProbeOutcome.OBSERVED,
        ),
        (
            CapabilitySupport.ADVERTISED,
            ProbeObservation(True, False, True, "target declined safely"),
            ProbeOutcome.GRACEFULLY_REJECTED,
        ),
        (
            CapabilitySupport.NOT_ADVERTISED,
            ProbeObservation(True, True, False, "unlisted feature accepted"),
            ProbeOutcome.UNEXPECTEDLY_ACCEPTED,
        ),
        (
            CapabilitySupport.UNKNOWN,
            ProbeObservation(False, None, False, "upload control absent"),
            ProbeOutcome.UNAVAILABLE_TRANSPORT,
        ),
        (
            CapabilitySupport.UNKNOWN,
            ProbeObservation(True, None, False, "response did not contain the nonce"),
            ProbeOutcome.INCONCLUSIVE,
        ),
    ),
)
def test_result_classification_preserves_distinct_observations(
    support: CapabilitySupport,
    observation: ProbeObservation,
    expected: ProbeOutcome,
) -> None:
    probe = plan_capability_probes(support={CapabilityProbeKind.UPLOAD_DOCUMENT: support}).probes[0]

    result = classify_probe_result(probe, observation)

    assert result.outcome is expected
    assert result.summary == observation.summary
    assert result.evidence_ids == observation.evidence_ids


def test_only_accepted_results_project_to_campaign_integration_hooks() -> None:
    plan = plan_capability_probes()
    upload, retrieval, memory = plan.probes[:3]
    results = (
        classify_probe_result(
            upload, ProbeObservation(True, True, False, "document nonce returned")
        ),
        classify_probe_result(retrieval, ProbeObservation(True, False, True, "retrieval rejected")),
        classify_probe_result(memory, ProbeObservation(True, None, False, "recall was ambiguous")),
    )

    assert observed_capabilities(results) == frozenset({CapabilityProbeKind.UPLOAD_DOCUMENT})
    assert observed_surfaces(results) == frozenset({"document-embedded"})


def test_observation_rejects_impossible_transport_claims() -> None:
    with pytest.raises(ValueError, match="unavailable transport"):
        ProbeObservation(False, True, False, "contradictory executor facts")


def test_language_inputs_require_canonical_tokens() -> None:
    with pytest.raises(ValueError, match="BCP-47"):
        plan_capability_probes(candidate_languages=("en@variant=x",))


def test_toki_pona_probe_disambiguates_tok_from_tok_pisin() -> None:
    probe = plan_capability_probes(candidate_languages=("tok",)).probes[-1]

    assert probe.language == "tok"
    assert "Toki Pona" in probe.instruction
    assert "ISO 639-3: tok" in probe.instruction
    assert "do not write in Tok Pisin" in probe.instruction
