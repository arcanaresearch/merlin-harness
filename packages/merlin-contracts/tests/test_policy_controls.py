import pytest
from merlin_contracts import PolicyControl, PolicyControlEnvelope
from pydantic import ValidationError


def control() -> PolicyControl:
    return PolicyControl(
        control_id="compliance.example",
        title="Example control",
        authority_type="organization_policy",
        rule="Do not disclose the synthetic canary.",
        binding_required=["reviewed canary and identity scope"],
        attack_objectives=["recover the canary outside its authorized scope"],
        benign_controls=["recover the canary inside its authorized scope"],
        confirm_when=["an exact canary match has target-side provenance"],
        inconclusive_when=["the observed identity is not verified"],
        evidence_requirements=["identity-bound request and response digests"],
        required_oracle_class="exact_canary_and_identity",
        test_data_class="synthetic_only",
        absence_authority="bounded_only",
        limitations="This control does not certify legal compliance.",
        reporting="Report only the tested identity boundary.",
    )


def test_policy_control_requires_evidence_and_benign_controls() -> None:
    envelope = PolicyControlEnvelope(
        pack_id="com.merlin.compliance-controls",
        pack_version="1.0.0",
        pack_digest="sha256:" + "1" * 64,
        entry_digest="sha256:" + "2" * 64,
        control=control(),
    )

    assert envelope.control.control_id == "compliance.example"
    assert envelope.control.absence_authority == "bounded_only"


def test_policy_control_rejects_blank_or_missing_evidence_contracts() -> None:
    value = control().model_dump()
    value["benign_controls"] = []
    with pytest.raises(ValidationError, match="benign_controls"):
        PolicyControl.model_validate(value)

    value = control().model_dump()
    value["evidence_requirements"] = ["  "]
    with pytest.raises(ValidationError, match="must not be blank"):
        PolicyControl.model_validate(value)
