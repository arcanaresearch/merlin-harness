from dataclasses import asdict, replace

import pytest
from merlin_agents import AssignmentTechnique
from merlin_agents.transforms import (
    PromptTransformSpec,
    materialize_transform_envelope,
    verify_transform_envelope,
)


def test_transform_spec_reproduces_exactly_and_rejects_tamper() -> None:
    envelope, spec = materialize_transform_envelope(
        "technique/policy",
        "Test the reviewed policy boundary",
        transform_id="translated",
        default_language="en",
        target_language="es",
        metamorphic_oracle_digest=None,
    )
    repeated, repeated_spec = materialize_transform_envelope(
        "technique/policy",
        "Test the reviewed policy boundary",
        transform_id="translated",
        default_language="en",
        target_language="es",
        metamorphic_oracle_digest=None,
    )

    assert (repeated, repeated_spec) == (envelope, spec)
    assert PromptTransformSpec.restore(asdict(spec)) == spec
    verify_transform_envelope(
        spec,
        source_id="technique/policy",
        source_prompt="Test the reviewed policy boundary",
        envelope_prompt=envelope,
    )
    with pytest.raises(ValueError, match="does not reproduce"):
        verify_transform_envelope(
            spec,
            source_id="technique/policy",
            source_prompt="changed source",
            envelope_prompt=envelope,
        )
    with pytest.raises(ValueError, match="unsupported prompt transform version"):
        PromptTransformSpec.restore(asdict(replace(spec, version="2.0.0")))
    tampered = asdict(spec)
    tampered["source_prompt_digest"] = "sha256:" + "f" * 64
    with pytest.raises(ValueError, match="specification digest mismatch"):
        PromptTransformSpec.restore(tampered)
    with pytest.raises(ValueError, match="differ from their sealed specification"):
        AssignmentTechnique(
            id="technique/policy@lang=es@variant=translated",
            objective=envelope + " changed",
            tags=frozenset(),
            guidance_ids=(),
            semantic_id="technique/policy",
            transform_id="translated",
            transform_spec=spec,
            language="es",
            comparison_supported=False,
            comparison_block_reason="metamorphic evaluator absent",
        )


def test_external_transform_is_rejected() -> None:
    with pytest.raises(ValueError, match="unsupported external"):
        materialize_transform_envelope(
            "technique/policy",
            "source",
            transform_id="third-party/model-prose",
            default_language="en",
            target_language="es",
            metamorphic_oracle_digest=None,
        )
