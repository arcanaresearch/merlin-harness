"""Deterministic execution strategy for reviewed target-state lifecycles."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from .target_capabilities import (
    StateAffordance,
    StateAffordanceKind,
    StateCapabilities,
    StateLifecycleIsolation,
)


class StateIsolationStrategy(StrEnum):
    FRESH = "fresh"
    RESTORE = "restore"
    CLONE = "clone"
    SERIALIZED = "serialized"


class StateIsolationPlan(BaseModel):
    """One evidence-bound strategy selected before target delivery."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    strategy: StateIsolationStrategy
    affordance_step_id: str | None = None
    maximum_parallel_states: int = Field(default=1, ge=1)
    requires_freshness_proof: bool = False
    requires_identity_comparison: bool = False
    isolation_disposition: str
    evidence_refs: tuple[str, ...] = ()
    fallback_reason: str | None = None

    @property
    def isolated(self) -> bool:
        return self.strategy is not StateIsolationStrategy.SERIALIZED

    @property
    def permits_parallel_delivery(self) -> bool:
        return self.isolated and self.maximum_parallel_states > 1


def resolve_state_isolation(
    capabilities: StateCapabilities | None,
    *,
    checkpoint_available: bool = False,
) -> StateIsolationPlan:
    """Choose fresh, restore, clone, or conservative serialized attribution.

    Versioned contracts must carry reviewed lifecycle, selector, affordance, and
    freshness evidence. Legacy compiled adapters retain their already-reviewed
    identity/freshness semantics, but still cannot claim checkpoint isolation
    without an available checkpoint.
    """

    if capabilities is None:
        return _serialized_plan(None, "state capabilities are unknown")
    identity = capabilities.identity
    requires_identity = identity is not None
    if identity is None:
        return _serialized_plan(capabilities, "physical-state identity is unreviewed")
    if capabilities.schema_version is not None:
        lifecycle = capabilities.lifecycle
        if lifecycle is None or lifecycle.isolation is not StateLifecycleIsolation.INDEPENDENT:
            return _serialized_plan(
                capabilities,
                "reviewed lifecycle does not establish independent state",
            )
        if not lifecycle.evidence_refs or not _selector_is_evidenced(identity.baseline_selector):
            return _serialized_plan(capabilities, "independent lifecycle evidence is incomplete")

    affordances = {item.kind: item for item in capabilities.affordances}
    ordered = (
        (StateIsolationStrategy.FRESH, StateAffordanceKind.PROVISION_FRESH_STATE),
        (StateIsolationStrategy.RESTORE, StateAffordanceKind.RESTORE_CHECKPOINT),
        (StateIsolationStrategy.CLONE, StateAffordanceKind.CLONE_STATE),
    )
    for strategy, kind in ordered:
        affordance = affordances.get(kind)
        if affordance is None or not _affordance_is_reviewed(capabilities, affordance):
            continue
        if strategy is StateIsolationStrategy.RESTORE and (
            not checkpoint_available or capabilities.checkpoint is None
        ):
            continue
        if not capabilities.freshness_rules or not all(
            _freshness_rule_is_reviewed(capabilities, rule) for rule in capabilities.freshness_rules
        ):
            continue
        maximum_parallel_states = capabilities.maximum_parallel_states or 1
        evidence_refs = _isolation_evidence_refs(capabilities, affordance)
        return StateIsolationPlan(
            strategy=strategy,
            affordance_step_id=affordance.step_id,
            maximum_parallel_states=maximum_parallel_states,
            requires_freshness_proof=True,
            requires_identity_comparison=True,
            isolation_disposition={
                StateIsolationStrategy.FRESH: "fresh",
                StateIsolationStrategy.RESTORE: "restored",
                StateIsolationStrategy.CLONE: "cloned",
            }[strategy],
            evidence_refs=evidence_refs,
        )
    return _serialized_plan(
        capabilities,
        "no executable independent-state affordance has complete reviewed evidence",
        requires_identity=requires_identity,
    )


def _serialized_plan(
    capabilities: StateCapabilities | None,
    reason: str,
    *,
    requires_identity: bool | None = None,
) -> StateIsolationPlan:
    return StateIsolationPlan(
        strategy=StateIsolationStrategy.SERIALIZED,
        maximum_parallel_states=1,
        requires_identity_comparison=(
            capabilities is not None and capabilities.identity is not None
            if requires_identity is None
            else requires_identity
        ),
        isolation_disposition="shared_serialized",
        fallback_reason=reason,
    )


def _affordance_is_reviewed(
    capabilities: StateCapabilities,
    affordance: StateAffordance,
) -> bool:
    return capabilities.schema_version is None or bool(
        affordance.evidence_refs and affordance.confidence is not None
    )


def _selector_is_evidenced(selector: object) -> bool:
    return bool(
        getattr(selector, "evidence_refs", ()) and getattr(selector, "confidence", None) is not None
    )


def _freshness_rule_is_reviewed(capabilities: StateCapabilities, rule: object) -> bool:
    if capabilities.schema_version is None:
        return True
    return bool(
        getattr(rule, "evidence_refs", ())
        and _selector_is_evidenced(getattr(rule, "selector", None))
    )


def _isolation_evidence_refs(
    capabilities: StateCapabilities,
    affordance: StateAffordance,
) -> tuple[str, ...]:
    values: list[str] = []
    lifecycle = capabilities.lifecycle
    if lifecycle is not None:
        values.extend(lifecycle.evidence_refs)
    values.extend(affordance.evidence_refs)
    identity = capabilities.identity
    if identity is not None:
        values.extend(identity.evidence_refs)
        values.extend(identity.baseline_selector.evidence_refs)
    for rule in capabilities.freshness_rules:
        values.extend(rule.evidence_refs)
        values.extend(rule.selector.evidence_refs)
    return tuple(dict.fromkeys(values))
