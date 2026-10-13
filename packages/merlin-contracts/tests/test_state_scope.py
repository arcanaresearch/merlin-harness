import pytest
from merlin_contracts.state_scope import (
    STATE_SCOPE_MIGRATION,
    STATE_SCOPE_ROLLBACK,
    StateScopeEnvelope,
    StateScopeEvidence,
    StateScopeMigrationContract,
)
from pydantic import ValidationError


def _present(ref: str) -> dict[str, object]:
    return {"availability": "present", "evidence_refs": [ref], "item_count": 1}


def _empty() -> dict[str, object]:
    return {"availability": "empty"}


def _missing(reason: str) -> dict[str, object]:
    return {"availability": "missing", "missing": {"reason": reason}}


def test_state_scope_requires_all_action_evidence_slots() -> None:
    envelope = StateScopeEnvelope.model_validate(
        {
            "scope_id": "scope-1",
            "campaign_id": "campaign-1",
            "experiment_id": "experiment-1",
            "attempt_id": "attempt-1",
            "action_id": "request-1",
            "action_kind": "candidate",
            "target_authority": "https://target.test",
            "candidate_id": "candidate-1",
            "principal_identity": {
                "kind": "opaque_reference",
                "namespace": "credential",
                "reference_id": "credential-1",
            },
            "physical_state_identity": {
                "kind": "keyed_digest",
                "algorithm": "hmac-sha256",
                "key_id": "identity-key-1",
                "digest": "a" * 64,
            },
            "before": _present("evidence:before"),
            "action": _present("evidence:request"),
            "responses": _present("evidence:response"),
            "after": _empty(),
        }
    )

    assert envelope.schema_version == "merlin.state-scope.v1"
    assert envelope.selector_contract_version == "merlin.state-selector.v1"
    assert envelope.after.availability == "empty"


def test_state_scope_rejects_raw_identity_values() -> None:
    with pytest.raises(ValidationError):
        StateScopeEnvelope.model_validate(
            {
                "scope_id": "scope-1",
                "campaign_id": "campaign-1",
                "experiment_id": "experiment-1",
                "attempt_id": "attempt-1",
                "action_id": "request-1",
                "action_kind": "baseline",
                "target_authority": "https://target.test",
                "physical_state_identity": "raw-target-session-id",
                "before": _empty(),
                "action": _present("evidence:request"),
                "responses": _missing("transport_failed"),
                "after": _missing("selector_unavailable"),
            }
        )


def test_missing_evidence_requires_typed_reason() -> None:
    with pytest.raises(ValidationError, match="typed missing reason"):
        StateScopeEvidence(availability="missing")


def test_present_evidence_accounts_for_every_captured_item() -> None:
    with pytest.raises(ValidationError, match="item_count must match"):
        StateScopeEvidence(
            availability="present",
            evidence_refs=("evidence:one", "evidence:two"),
            item_count=1,
        )


def test_empty_snapshot_is_distinct_from_missing_evidence() -> None:
    empty = StateScopeEvidence(availability="empty")
    missing = StateScopeEvidence.model_validate(_missing("target_omitted"))

    assert empty.missing is None
    assert missing.missing is not None
    assert missing.missing.reason == "target_omitted"


def test_candidate_scope_requires_candidate_id() -> None:
    with pytest.raises(ValidationError, match="candidate_id"):
        StateScopeEnvelope.model_validate(
            {
                "scope_id": "scope-1",
                "campaign_id": "campaign-1",
                "experiment_id": "experiment-1",
                "attempt_id": "attempt-1",
                "action_id": "request-1",
                "action_kind": "candidate",
                "target_authority": "https://target.test",
                "before": _empty(),
                "action": _present("evidence:request"),
                "responses": _missing("deadline_expired"),
                "after": _missing("capture_incomplete"),
            }
        )


def test_migration_and_rollback_contracts_forbid_dual_execution_and_downgrade() -> None:
    assert STATE_SCOPE_MIGRATION.dual_execution is False
    assert STATE_SCOPE_MIGRATION.preserve_legacy_digests is True
    assert STATE_SCOPE_ROLLBACK.pause_new_format_campaigns is True
    assert STATE_SCOPE_ROLLBACK.downgrade_new_format_campaigns is False

    with pytest.raises(ValidationError):
        StateScopeMigrationContract(dual_execution=True)
