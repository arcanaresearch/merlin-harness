"""Deterministic, digest-sealed prompt transform specifications."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

ENGINE_VERSION = "1.0.0"
TEMPLATES = {
    "native": (
        "Compose and reason natively in {target_language}; do not translate from "
        "{default_language}."
    ),
    "translated": (
        "Translate from {default_language} into {target_language} while preserving the "
        "reviewed oracle."
    ),
    "mixed": "Mix {default_language} and {target_language} while preserving the reviewed oracle.",
    "script-switching": (
        "Use script-switching or transliterated {target_language} without changing the "
        "reviewed oracle."
    ),
}


def digest_json(value: object) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return "sha256:" + hashlib.sha256(encoded.encode()).hexdigest()


def digest_text(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode()).hexdigest()


def implementation_digest(transform_id: str) -> str:
    template = TEMPLATES.get(transform_id)
    if template is None:
        raise ValueError(f"unsupported external prompt transform: {transform_id}")
    return digest_json(
        {
            "engine": "merlin.deterministic-transform-envelope",
            "version": ENGINE_VERSION,
            "transform_id": transform_id,
            "template": template,
        }
    )


@dataclass(frozen=True, slots=True)
class PromptTransformSpec:
    """Reviewed transform contract; generated payloads require a concrete effect receipt."""

    transform_id: str
    version: str
    parameters: tuple[tuple[str, str], ...]
    source_id: str
    source_prompt_digest: str
    implementation_digest: str
    envelope_prompt_digest: str
    metamorphic_oracle_digest: str | None
    spec_digest: str
    language: str
    language_mode: str

    def __post_init__(self) -> None:
        if self.transform_id not in TEMPLATES:
            raise ValueError(f"unsupported external prompt transform: {self.transform_id}")
        if self.version != ENGINE_VERSION:
            raise ValueError(f"unsupported prompt transform version: {self.version}")
        if self.language_mode != self.transform_id:
            raise ValueError("transform language mode differs from transform id")
        names = tuple(name for name, _ in self.parameters)
        if names != tuple(sorted(names)) or len(names) != len(set(names)):
            raise ValueError("transform parameters must be uniquely sorted")
        digests = (
            self.source_prompt_digest,
            self.implementation_digest,
            self.envelope_prompt_digest,
            self.spec_digest,
        )
        if any(not value.startswith("sha256:") for value in digests):
            raise ValueError("transform identities must be SHA-256 digests")
        if self.metamorphic_oracle_digest is not None and not (
            self.metamorphic_oracle_digest.startswith("sha256:")
        ):
            raise ValueError("transform metamorphic oracle must be a SHA-256 digest")
        if self.implementation_digest != implementation_digest(self.transform_id):
            raise ValueError("prompt transform implementation digest changed")
        unsigned = {
            "transform_id": self.transform_id,
            "version": self.version,
            "parameters": self.parameters,
            "source_id": self.source_id,
            "source_prompt_digest": self.source_prompt_digest,
            "implementation_digest": self.implementation_digest,
            "envelope_prompt_digest": self.envelope_prompt_digest,
            "metamorphic_oracle_digest": self.metamorphic_oracle_digest,
            "language": self.language,
            "language_mode": self.language_mode,
        }
        if self.spec_digest != digest_json(unsigned):
            raise ValueError("prompt transform specification digest mismatch")

    @classmethod
    def restore(cls, value: Mapping[str, Any]) -> PromptTransformSpec:
        return cls(
            transform_id=str(value["transform_id"]),
            version=str(value["version"]),
            parameters=tuple((str(item[0]), str(item[1])) for item in value["parameters"]),
            source_id=str(value["source_id"]),
            source_prompt_digest=str(value["source_prompt_digest"]),
            implementation_digest=str(value["implementation_digest"]),
            envelope_prompt_digest=str(value["envelope_prompt_digest"]),
            metamorphic_oracle_digest=(
                str(value["metamorphic_oracle_digest"])
                if value.get("metamorphic_oracle_digest") is not None
                else None
            ),
            spec_digest=str(value["spec_digest"]),
            language=str(value["language"]),
            language_mode=str(value["language_mode"]),
        )


def materialize_transform_envelope(
    source_id: str,
    source_prompt: str,
    *,
    transform_id: str,
    default_language: str,
    target_language: str,
    metamorphic_oracle_digest: str | None,
) -> tuple[str, PromptTransformSpec]:
    """Materialize the exact deterministic assignment envelope and its specification."""
    if not source_id.strip() or not source_prompt.strip():
        raise ValueError("transform requires a non-empty exact source")
    template = TEMPLATES.get(transform_id)
    if template is None:
        raise ValueError(f"unsupported external prompt transform: {transform_id}")
    parameters = tuple(
        sorted(
            {
                "default_language": default_language.strip().casefold(),
                "target_language": target_language.strip().casefold(),
            }.items()
        )
    )
    if any(not value for _name, value in parameters):
        raise ValueError("transform language parameters must not be blank")
    values = dict(parameters)
    envelope = (
        f"{source_prompt}\n\nLanguage cell: {values['target_language']}; variant: "
        f"{transform_id}. {template.format(**values)} Preserve the same assessment "
        "semantics; generated target payloads remain provisional until receipt-bound."
    )
    unsigned = {
        "transform_id": transform_id,
        "version": ENGINE_VERSION,
        "parameters": parameters,
        "source_id": source_id,
        "source_prompt_digest": digest_text(source_prompt),
        "implementation_digest": implementation_digest(transform_id),
        "envelope_prompt_digest": digest_text(envelope),
        "metamorphic_oracle_digest": metamorphic_oracle_digest,
        "language": values["target_language"],
        "language_mode": transform_id,
    }
    return envelope, PromptTransformSpec(**unsigned, spec_digest=digest_json(unsigned))


def verify_transform_envelope(
    spec: PromptTransformSpec,
    *,
    source_id: str,
    source_prompt: str,
    envelope_prompt: str,
) -> None:
    params = dict(spec.parameters)
    reproduced, expected = materialize_transform_envelope(
        source_id,
        source_prompt,
        transform_id=spec.transform_id,
        default_language=params.get("default_language", ""),
        target_language=params.get("target_language", ""),
        metamorphic_oracle_digest=spec.metamorphic_oracle_digest,
    )
    if reproduced != envelope_prompt or expected != spec:
        raise ValueError("transform specification does not reproduce exactly")
