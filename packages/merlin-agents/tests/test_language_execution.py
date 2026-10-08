from dataclasses import asdict

from merlin_agents.language_execution import (
    LanguageExecutionEvidence,
    language_reachability,
    record_language_execution,
)
from merlin_agents.mutation_execution import production_mutation_schedule


def test_language_evidence_records_requested_and_actual_without_inference() -> None:
    evidence = record_language_execution(
        "source",
        "salida",
        requested_transform_id="translated",
        applied_transform_id="translated",
        requested_language="ES",
        actual_language="en",
        executor_id="provider:route:model",
    )

    assert evidence.requested_language == "es"
    assert evidence.actual_language == "en"
    assert evidence.language_match is False
    assert LanguageExecutionEvidence(**asdict(evidence)) == evidence
    mismatch = record_language_execution(
        "source",
        "salida",
        requested_transform_id="translated",
        applied_transform_id="native",
        requested_language="es",
        actual_language="es",
        executor_id="provider:route:model",
    )
    assert mismatch.verification_status == "mismatch"
    assert mismatch.execution_class == "unavailable"
    assert mismatch.transform_credit_eligible is False
    unverifiable = record_language_execution(
        "source",
        "salida",
        requested_transform_id="translated",
        applied_transform_id=None,
        requested_language="es",
        actual_language="es",
        executor_id="provider:route:model",
    )
    assert unverifiable.verification_status == "unverifiable"
    assert unverifiable.transform_credit_eligible is False
    unknown_language = record_language_execution(
        "source",
        "12345",
        requested_transform_id="numbers-only",
        applied_transform_id=None,
        requested_language="en",
        actual_language="und",
        executor_id="provider:route:model",
    )
    assert unknown_language.actual_language == "und"
    assert unknown_language.verification_status == "unverifiable"


def test_language_reachability_does_not_claim_a_local_translator() -> None:
    reach = language_reachability("translated")

    assert reach.production_reachable is True
    assert reach.locally_executable is False
    assert "attestation" in reach.reason


def test_schedule_covers_meaningful_diversity_and_every_row_is_reachable_or_honestly_blocked() -> (
    None
):
    schedule = production_mutation_schedule()

    assert {item.audience for item in schedule} == {
        "mainstream",
        "low-resource",
        "classical",
        "mixed",
        "encoded",
        "adversarial",
    }
    assert any(
        item.strategy_id == "register:wenyanwen" and item.requested_language == "lzh"
        for item in schedule
    )
    assert any(
        item.strategy_id == "register:amharic" and item.requested_language == "am"
        for item in schedule
    )
    assert any(item.audience == "mixed" and item.requested_language == "mul" for item in schedule)
    assert all(item.production_reachable or not item.locally_executable for item in schedule)
    assert all(item.execution_path or not item.production_reachable for item in schedule)
