"""Versioned resource bounds for model-backed candidate generators."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

GENERATOR_EXECUTION_SCHEMA_VERSION = "merlin.generator-execution.v1"


class GeneratorExecutionContract(BaseModel):
    """Hard output and wall-clock limits for one generator invocation."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["merlin.generator-execution.v1"] = GENERATOR_EXECUTION_SCHEMA_VERSION
    generator_id: str = Field(min_length=1)
    max_outputs: int = Field(strict=True, ge=1)
    max_output_bytes: int = Field(strict=True, ge=1)
    max_seconds: float = Field(strict=True, gt=0)
