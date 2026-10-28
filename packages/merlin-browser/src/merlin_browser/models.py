from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any, Literal

from merlin_contracts.target_adapter_compiler import (
    compiled_target_adapter_digest,
    read_target_adapter,
)
from pydantic import BaseModel, ConfigDict, Field, model_validator

_SECRET_SCOPE_WORDS = ("api_key", "authorization", "credential", "password", "secret", "token")


def _contains_secret_key(value: object) -> bool:
    if isinstance(value, Mapping):
        return any(
            any(word in str(key).lower() for word in _SECRET_SCOPE_WORDS)
            or _contains_secret_key(item)
            for key, item in value.items()
        )
    if isinstance(value, (list, tuple)):
        return any(_contains_secret_key(item) for item in value)
    return False


def _legacy_browser_text_contract_can_upgrade(
    persisted: object,
    persisted_compilation: object,
    compatibility_compilation: object,
) -> bool:
    if not isinstance(persisted, Mapping):
        return False
    persisted_executable = persisted.get("executable", persisted)
    if not isinstance(persisted_executable, Mapping):
        return False
    persisted_interaction = persisted_executable.get("interaction")
    if not isinstance(persisted_interaction, Mapping):
        return False
    if any(
        field in persisted_interaction for field in ("response_witness", "state_observations")
    ):
        return False

    old_executable = getattr(persisted_compilation, "executable", None)
    new_executable = getattr(compatibility_compilation, "executable", None)
    if getattr(old_executable, "kind", None) != "browser_chat" or getattr(
        new_executable, "kind", None
    ) != "browser_chat":
        return False
    old_payload = old_executable.model_dump(mode="json")
    new_payload = new_executable.model_dump(mode="json")
    for payload in (old_payload, new_payload):
        interaction = payload.get("interaction")
        if not isinstance(interaction, dict):
            return False
        interaction.pop("response_witness", None)
        interaction.pop("state_observations", None)
    return old_payload == new_payload


API_FIRST_RECONNAISSANCE_GUIDANCE = (
    "Inspect attributable network evidence first. Prefer a bounded, reproducible API adapter "
    "when it preserves the same target behavior; retain DOM interaction only when no equivalent "
    "API surface exists, and record that fallback reason."
)


class BrowserModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class BrowserBounds(BrowserModel):
    target_id: str
    allowed_origins: frozenset[str]
    allow_downloads: bool = False
    allow_uploads: bool = False
    allowed_upload_paths: frozenset[str] = frozenset()
    max_tabs: int = Field(default=4, ge=1, le=16)
    requires_persistent_profile: bool = False
    single_chat_session: bool = False


class BrowserOwner(BrowserModel):
    session_id: str
    worker_id: str
    technique_id: str


class ElementRef(BrowserModel):
    ref: str
    tab_ref: str
    frame_ref: str
    role: str
    name: str
    input_type: str | None = None
    max_length: int | None = Field(default=None, ge=1)
    disabled: bool = False
    read_only: bool = False


class BrowserObservation(BrowserModel):
    context_id: str
    target_id: str
    origin: str
    owner: BrowserOwner
    tab_ref: str
    frame_ref: str | None = None
    kind: Literal["snapshot", "navigation", "network", "popup", "blocker", "cleanup"]
    step: int = Field(default=0, ge=0)
    text: str = ""
    elements: tuple[ElementRef, ...] = ()
    evidence_refs: tuple[str, ...] = ()
    redaction_status: Literal["not_needed", "redacted"] = "not_needed"
    observed_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class BrowserAction(BrowserModel):
    context_id: str
    target_id: str
    owner: BrowserOwner
    kind: Literal[
        "open_tab",
        "navigate",
        "click",
        "fill",
        "press",
        "select",
        "check",
        "uncheck",
        "hover",
        "wait_for_text",
        "network_events",
        "upload",
        "snapshot",
        "close_tab",
    ]
    tab_ref: str
    frame_ref: str | None = None
    element_ref: str | None = None
    value: str | None = None
    timeout_ms: int = Field(default=10_000, ge=100, le=30_000)
    step: int = Field(ge=0)
    evidence_refs: tuple[str, ...] = ()
    redaction_status: Literal["not_needed", "redacted"] = "not_needed"
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class TabMetadata(BrowserModel):
    tab_ref: str
    url: str
    opener_ref: str | None = None
    state: Literal["open", "closed", "blocked"] = "open"


class BrowserMetadata(BrowserModel):
    schema_version: Literal["merlin.browser.metadata.v1"] = "merlin.browser.metadata.v1"
    context_id: str
    mode: Literal["isolated", "attached", "persistent"]
    target_id: str
    owner: BrowserOwner
    allowed_origins: frozenset[str]
    # Vendor origins admitted at runtime because they were reached from an
    # in-bounds frame (embedded widgets, CDN subresources). Audit trail only;
    # never feeds back into allowed_origins.
    admitted_frame_origins: tuple[str, ...] = ()
    tabs: tuple[TabMetadata, ...]
    state: Literal["active", "cleaned", "restart_required"]
    active_actor_id: str | None = None
    launch_mode: Literal["headful", "headless", "attached", "unknown"] = "unknown"
    launch_fallback_reason: str | None = None
    created_at: datetime
    updated_at: datetime


class ApiReconHandoff(BrowserModel):
    id: str
    context_id: str
    target_id: str
    owner: BrowserOwner
    method: str
    url: str
    request_evidence_ref: str
    response_evidence_ref: str
    adapter: str | None = None
    dom_fallback_reason: str | None = None
    reusable_adapter: str | None = None
    compiled_adapter: str | None = None
    compiled_adapter_digest: str | None = None
    reusable_site_facts: str | None = None
    recon_scope: dict[str, str] = Field(default_factory=dict)
    recon_observations: tuple[dict[str, Any], ...] = ()
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @model_validator(mode="after")
    def explicit_api_or_dom_fallback(self) -> ApiReconHandoff:
        if not self.adapter and not self.dom_fallback_reason:
            raise ValueError("handoff requires an API adapter or DOM fallback reason")
        if self.adapter is not None:
            try:
                value = json.loads(self.adapter)
            except json.JSONDecodeError as error:
                raise ValueError("API handoff adapter must be canonical JSON") from error
            if not isinstance(value, Mapping) or not value:
                raise ValueError("API handoff adapter must be a non-empty JSON object")
            canonical = json.dumps(value, sort_keys=True, separators=(",", ":"))
            if self.adapter != canonical:
                raise ValueError("API handoff adapter must use canonical JSON encoding")
        if self.reusable_adapter is not None:
            try:
                reusable = json.loads(self.reusable_adapter)
            except json.JSONDecodeError as error:
                raise ValueError("reusable adapter must be canonical JSON") from error
            if not isinstance(reusable, Mapping) or not reusable:
                raise ValueError("reusable adapter must be a non-empty JSON object")
            canonical = json.dumps(reusable, sort_keys=True, separators=(",", ":"))
            if self.reusable_adapter != canonical:
                raise ValueError("reusable adapter must use canonical JSON encoding")
        if bool(self.compiled_adapter) != bool(self.compiled_adapter_digest):
            raise ValueError("compiled adapter serialization and digest must be stored together")
        if self.compiled_adapter is not None:
            try:
                persisted_compiled = json.loads(self.compiled_adapter)
                compilation = read_target_adapter(self.compiled_adapter)
            except (json.JSONDecodeError, ValueError) as error:
                raise ValueError("compiled adapter must be a valid canonical contract") from error
            canonical = json.dumps(
                compilation.model_dump(mode="json"),
                sort_keys=True,
                separators=(",", ":"),
            )
            if self.compiled_adapter != canonical:
                # Older sealed handoffs can omit fields that were later added
                # with deterministic defaults (for example request_bindings).
                # ``read_target_adapter`` has already validated the contract;
                # normalize that legacy spelling before checking its semantic
                # digest so durable reconnaissance remains reusable.
                object.__setattr__(self, "compiled_adapter", canonical)
            observed_digest = compiled_target_adapter_digest(compilation)
            if self.compiled_adapter_digest != observed_digest:
                raise ValueError("compiled adapter digest does not match executable semantics")
            # adapter is authoritative when both branches coexist; the browser
            # reusable_adapter is retained as DOM-fallback evidence only.
            legacy = self.adapter or self.reusable_adapter
            if legacy is None:
                raise ValueError("compiled adapter requires its v1 compatibility projection")
            try:
                compatibility_compilation = read_target_adapter(legacy)
                legacy_digest = compiled_target_adapter_digest(compatibility_compilation)
            except ValueError as error:
                raise ValueError("v1 compatibility adapter is not executable") from error
            if legacy_digest != observed_digest:
                if not _legacy_browser_text_contract_can_upgrade(
                    persisted_compiled,
                    compilation,
                    compatibility_compilation,
                ):
                    raise ValueError("compiled adapter and v1 compatibility projection disagree")
                upgraded = json.dumps(
                    compatibility_compilation.model_dump(mode="json"),
                    sort_keys=True,
                    separators=(",", ":"),
                )
                object.__setattr__(self, "compiled_adapter", upgraded)
                object.__setattr__(self, "compiled_adapter_digest", legacy_digest)
        if self.reusable_site_facts is not None:
            try:
                site_facts = json.loads(self.reusable_site_facts)
            except json.JSONDecodeError as error:
                raise ValueError("reusable site facts must be canonical JSON") from error
            if not isinstance(site_facts, Mapping) or not site_facts:
                raise ValueError("reusable site facts must be a non-empty JSON object")
            if _contains_secret_key(site_facts):
                raise ValueError("reusable site facts must not contain credential-like fields")
            canonical = json.dumps(site_facts, sort_keys=True, separators=(",", ":"))
            if self.reusable_site_facts != canonical:
                raise ValueError("reusable site facts must use canonical JSON encoding")
        allowed_scope = {
            "surface",
            "authentication_context",
            "tenant",
            "agent_or_bot",
            "scenario",
            "session_constraint",
            "oracle_contract",
        }
        if set(self.recon_scope) - allowed_scope or any(
            not isinstance(value, str) or not value.strip() for value in self.recon_scope.values()
        ):
            raise ValueError("recon scope contains unsupported or empty dimensions")
        if _contains_secret_key(self.recon_observations):
            raise ValueError("recon observations must not contain credential-like fields")
        allowed_recon_observation_fields = {
            "schema",
            "name",
            "role",
            "baseline",
            "result",
            "baseline_pointer",
            "result_pointer",
            "outcome",
            "success_value_matched",
            "result_observed_at",
            "evidence_refs",
            "digest",
        }
        for observation in self.recon_observations:
            if set(observation) != allowed_recon_observation_fields:
                raise ValueError("recon observation fields are incomplete or unsupported")
            if observation.get("schema") != "merlin.browser.recon-observation.v1":
                raise ValueError("recon observation schema is unsupported")
            if observation.get("outcome") not in {"observed", "partial"}:
                raise ValueError("recon observation outcome is unsupported")
            try:
                observed_at = datetime.fromisoformat(
                    str(observation["result_observed_at"]).replace("Z", "+00:00")
                )
            except (KeyError, ValueError) as error:
                raise ValueError("recon observation timestamp is invalid") from error
            if observed_at.tzinfo is None:
                raise ValueError("recon observation timestamp must include a timezone")
            evidence_refs = observation.get("evidence_refs")
            if not isinstance(evidence_refs, list) or not evidence_refs or any(
                not isinstance(reference, str)
                or not re.fullmatch(
                    r"browser-api-(?:request|response):sha256:[0-9a-f]{64}", reference
                )
                for reference in evidence_refs
            ):
                raise ValueError("recon observation evidence references are invalid")
            core = {key: value for key, value in observation.items() if key != "digest"}
            expected_digest = "sha256:" + hashlib.sha256(
                json.dumps(core, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest()
            if observation.get("digest") != expected_digest:
                raise ValueError("recon observation digest does not match its content")
        return self
