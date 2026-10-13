import pytest
from merlin_contracts.generation import GeneratorExecutionContract
from pydantic import ValidationError


def test_generator_execution_requires_output_size_count_and_time_bounds() -> None:
    contract = GeneratorExecutionContract(
        generator_id="mutation",
        max_outputs=8,
        max_output_bytes=65536,
        max_seconds=10.0,
    )
    assert contract.schema_version == "merlin.generator-execution.v1"

    for field, value in (
        ("max_outputs", 0),
        ("max_output_bytes", 0),
        ("max_seconds", 0.0),
    ):
        payload = contract.model_dump(mode="json")
        payload[field] = value
        with pytest.raises(ValidationError):
            GeneratorExecutionContract.model_validate(payload)


def test_generator_bounds_reject_boolean_coercion() -> None:
    with pytest.raises(ValidationError):
        GeneratorExecutionContract(
            generator_id="mutation",
            max_outputs=True,
            max_output_bytes=1024,
            max_seconds=1.0,
        )
