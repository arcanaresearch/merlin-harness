from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from importlib.resources import files
from typing import Any

from merlin_contracts import PolicyControl, PolicyControlEnvelope

from ._bundle_crypto import decrypt_builtin_pack

PACK_FILES = (
    "attack-catalogue.rpk",
    "benign-controls.rpk",
    "compliance-controls.rpk",
    "emerging-techniques.rpk",
    "industry-profiles.rpk",
    "interaction-guidance.rpk",
    "pricing-schedules.rpk",
    "target-capabilities.rpk",
)
MAX_SELECTED_ENTRIES = 12


@dataclass(frozen=True, slots=True)
class KnowledgeEntry:
    pack_id: str
    pack_version: str
    pack_digest: str
    entry_id: str
    entry_digest: str
    kind: str
    content: str
    specialist_tags: tuple[str, ...]
    target_tags: tuple[str, ...]
    applicability_tags: tuple[str, ...]
    provider_visibility: str

    @property
    def routing_terms(self) -> frozenset[str]:
        values = (
            self.entry_id,
            self.kind,
            *self.specialist_tags,
            *self.target_tags,
            *self.applicability_tags,
        )
        return frozenset(term for value in values for term in _terms(value))


@dataclass(frozen=True, slots=True)
class KnowledgePack:
    id: str
    version: str
    digest: str
    kind: str
    entries: tuple[KnowledgeEntry, ...]


def load_knowledge_packs() -> tuple[KnowledgePack, ...]:
    """Load and authenticate the eight canonical Rust knowledge-pack assets."""
    root = files("merlin_packs").joinpath("knowledge")
    packs = []
    for name in PACK_FILES:
        encrypted = root.joinpath(name + ".enc")
        if encrypted.is_file():
            payload = decrypt_builtin_pack(name, encrypted.read_bytes())
        else:
            payload = root.joinpath(name).read_text(encoding="utf-8")
        packs.append(_load_pack(payload))
    packs = tuple(packs)
    ids = [pack.id for pack in packs]
    if len(ids) != len(set(ids)):
        raise ValueError("bundled knowledge-pack identifiers must be unique")
    return packs


def knowledge_catalogue() -> tuple[dict[str, Any], ...]:
    return tuple(
        {
            "pack_id": pack.id,
            "pack_version": pack.version,
            "pack_digest": pack.digest,
            "kind": pack.kind,
            "entry_count": len(pack.entries),
        }
        for pack in load_knowledge_packs()
    )


def policy_control_catalogue() -> tuple[PolicyControlEnvelope, ...]:
    """Return authenticated, machine-validated policy controls."""
    return tuple(
        PolicyControlEnvelope(
            pack_id=entry.pack_id,
            pack_version=entry.pack_version,
            pack_digest=entry.pack_digest,
            entry_digest=entry.entry_digest,
            control=PolicyControl.model_validate_json(entry.content),
        )
        for pack in load_knowledge_packs()
        for entry in pack.entries
        if entry.kind == "compliance_control"
    )


def select_knowledge(
    objective: str, *, limit: int = MAX_SELECTED_ENTRIES
) -> tuple[KnowledgeEntry, ...]:
    """Select a bounded relevant body subset while the metadata catalogue stays shared."""
    if not 1 <= limit <= MAX_SELECTED_ENTRIES:
        raise ValueError(f"knowledge selection limit must be between 1 and {MAX_SELECTED_ENTRIES}")
    wanted = _terms(objective)
    entries = [entry for pack in load_knowledge_packs() for entry in pack.entries]
    ranked = sorted(
        entries,
        key=lambda entry: (-len(wanted & entry.routing_terms), entry.pack_id, entry.entry_id),
    )
    relevant = [entry for entry in ranked if wanted & entry.routing_terms]
    return tuple((relevant or ranked)[:limit])


def _load_pack(payload: str) -> KnowledgePack:
    value = json.loads(payload)
    if value.get("schema") != "merlin.knowledge-pack.v1":
        raise ValueError("unsupported bundled knowledge-pack schema")
    manifest, entries = value.get("manifest"), value.get("entries")
    if not isinstance(manifest, dict) or not isinstance(entries, list):
        raise ValueError("knowledge pack must contain a manifest and entries")
    pack_id = _required_string(manifest, "pack_id")
    pack_version = _required_string(manifest, "pack_version")
    pack_digest = _digest(manifest, "content_digest")
    parsed = []
    for raw in entries:
        if not isinstance(raw, dict):
            raise ValueError(f"knowledge pack {pack_id} contains a non-object entry")
        content = _required_string(raw, "content")
        entry_id, entry_digest = _required_string(raw, "entry_id"), _digest(raw, "digest")
        if "sha256:" + hashlib.sha256(content.encode()).hexdigest() != entry_digest:
            raise ValueError(f"knowledge entry {entry_id} digest mismatch")
        kind = _required_string(raw, "kind")
        if kind == "compliance_control":
            control = PolicyControl.model_validate_json(content)
            if control.control_id != entry_id:
                raise ValueError(
                    f"policy control {control.control_id} does not match entry {entry_id}"
                )
        parsed.append(
            KnowledgeEntry(
                pack_id,
                pack_version,
                pack_digest,
                entry_id,
                entry_digest,
                kind,
                content,
                _strings(raw, "specialist_tags"),
                _strings(raw, "target_tags"),
                _strings(raw, "applicability_tags"),
                _required_string(raw, "provider_visibility"),
            )
        )
    return KnowledgePack(
        pack_id, pack_version, pack_digest, _required_string(manifest, "kind"), tuple(parsed)
    )


def _required_string(value: dict[str, Any], key: str) -> str:
    observed = value.get(key)
    if not isinstance(observed, str) or not observed:
        raise ValueError(f"knowledge-pack {key} must be a non-empty string")
    return observed


def _digest(value: dict[str, Any], key: str) -> str:
    observed = _required_string(value, key)
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", observed):
        raise ValueError(f"knowledge-pack {key} must be a sha256 digest")
    return observed


def _strings(value: dict[str, Any], key: str) -> tuple[str, ...]:
    observed = value.get(key, [])
    if not isinstance(observed, list) or not all(isinstance(item, str) for item in observed):
        raise ValueError(f"knowledge-pack {key} must be a string list")
    return tuple(observed)


def _terms(value: str) -> frozenset[str]:
    return frozenset(re.findall(r"[a-z0-9]+", value.casefold()))
