"""Transport-neutral lifecycle for one durable target state transition."""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol

from .state_scope import StateScopeActionKind


class StateScopeStore(Protocol):
    def claim_state_scope_lease(
        self,
        scope_id: str,
        *,
        target_authority: str,
        principal_key: str,
        equivalence_key: str,
        claimant_id: str,
        lease_seconds: int,
    ) -> str | None: ...

    def renew_state_scope_lease(
        self,
        claim_id: str,
        claimant_id: str,
        lease_seconds: int,
    ) -> bool: ...

    def rekey_state_scope_lease(
        self,
        claim_id: str,
        claimant_id: str,
        *,
        principal_key: str,
        equivalence_key: str,
    ) -> bool: ...

    def release_state_scope_lease(self, claim_id: str, claimant_id: str) -> bool: ...

    def reobserve_state_lane(
        self,
        scope_id: str,
        expected_revision: int,
        evidence_refs: Sequence[str],
        *,
        claim_id: str,
        claimant_id: str,
        principal_identity: Mapping[str, object] | None = None,
        physical_state_identity: Mapping[str, object] | None = None,
    ) -> Mapping[str, object]: ...

    def create_state_scope(
        self,
        campaign_id: str,
        target_authority: str,
        envelope: Mapping[str, object],
        *,
        isolation_disposition: str = "unknown",
    ) -> Mapping[str, object]: ...

    def revise_state_scope(
        self,
        scope_id: str,
        expected_revision: int,
        envelope: Mapping[str, object],
        *,
        claim_id: str,
        claimant_id: str,
    ) -> Mapping[str, object]: ...

    def acknowledge_state_scope(
        self, scope_id: str, *, claim_id: str, claimant_id: str
    ) -> Mapping[str, object]: ...

    def settle_state_scope(
        self,
        scope_id: str,
        expected_revision: int,
        envelope: Mapping[str, object],
        *,
        transition_disposition: str,
        isolation_disposition: str,
        cleanup_disposition: str,
        claim_id: str,
        claimant_id: str,
    ) -> Mapping[str, object]: ...

    def pause_state_scope(
        self,
        scope_id: str,
        expected_revision: int,
        envelope: Mapping[str, object],
        *,
        isolation_disposition: str,
        cleanup_disposition: str,
        transition_disposition: str,
        claim_id: str,
        claimant_id: str,
    ) -> Mapping[str, object]: ...

    def pause_unclaimed_state_scope(
        self,
        scope_id: str,
        expected_revision: int,
        envelope: Mapping[str, object],
        *,
        transition_disposition: str,
        isolation_disposition: str,
        cleanup_disposition: str,
    ) -> Mapping[str, object]: ...


class CheckpointVaultEntry(Protocol):
    """Secret-safe projection returned by a checkpoint vault."""

    digest: str

    def opaque_reference(self) -> Mapping[str, str]: ...


class CheckpointVault(Protocol):
    """Owner-only target checkpoint secret storage."""

    def store_checkpoint(
        self,
        checkpoint_secret: str,
        *,
        campaign_id: str,
        gate_id: str,
    ) -> CheckpointVaultEntry: ...

    def resolve_checkpoint(self, reference_id: str) -> str: ...

    def digest_identity(self, value: object, *, purpose: str) -> Mapping[str, str]: ...


class ObjectiveCheckpointStore(Protocol):
    """Durable checkpoint binding fenced by one active state-scope lease."""

    def record_objective_gate_checkpoint(
        self,
        campaign_id: str,
        gate_id: str,
        *,
        manifest_digest: str,
        recipe_digest: str,
        checkpoint_handle: Mapping[str, object],
        evidence_digest: str,
        scope_id: str,
        claim_id: str,
        claimant_id: str,
    ) -> Mapping[str, object]: ...

    def claim_objective_gate_checkpoint_restore(
        self,
        campaign_id: str,
        gate_id: str,
        *,
        scope_id: str,
        claim_id: str,
        claimant_id: str,
    ) -> Mapping[str, object]: ...

    def release_objective_gate_checkpoint_restore(
        self,
        campaign_id: str,
        gate_id: str,
        *,
        scope_id: str,
        claim_id: str,
        claimant_id: str,
    ) -> bool: ...

    def mark_objective_gate_checkpoint_restored(
        self,
        campaign_id: str,
        gate_id: str,
        *,
        scope_id: str,
        claim_id: str,
        claimant_id: str,
        evidence_digest: str,
    ) -> Mapping[str, object]: ...


def state_scope_evidence(
    evidence_refs: Sequence[str] = (),
    *,
    missing_reason: str | None = None,
    retryable: bool = False,
) -> dict[str, object]:
    refs = tuple(dict.fromkeys(item for item in evidence_refs if item))
    if refs:
        return {
            "availability": "present",
            "evidence_refs": list(refs),
            "item_count": len(refs),
        }
    if missing_reason is not None:
        return {
            "availability": "missing",
            "evidence_refs": [],
            "item_count": 0,
            "missing": {"reason": missing_reason, "retryable": retryable},
        }
    return {"availability": "empty", "evidence_refs": [], "item_count": 0}


@dataclass(slots=True)
class StateScopeLifecycle:
    store: StateScopeStore
    campaign_id: str
    target_authority: str
    scope_id: str
    attempt_id: str
    action_id: str
    candidate_id: str
    experiment_id: str
    action_kind: StateScopeActionKind = StateScopeActionKind.CANDIDATE
    revision: int = 1
    claim_id: str | None = None
    claimant_id: str | None = None
    acknowledged: bool = False
    finished: bool = False
    principal_identity: Mapping[str, object] | None = None
    physical_state_identity: Mapping[str, object] | None = None
    lane_revision: int | None = None

    @classmethod
    async def create(
        cls,
        store: StateScopeStore,
        *,
        campaign_id: str,
        target_authority: str,
        attempt_id: str,
        action_id: str,
        candidate_id: str,
        experiment_id: str | None = None,
        action_kind: StateScopeActionKind = StateScopeActionKind.CANDIDATE,
    ) -> StateScopeLifecycle:
        scope_id = (
            "state-scope:" + hashlib.sha256(f"{campaign_id}\0{action_id}".encode()).hexdigest()
        )
        lifecycle = cls(
            store=store,
            campaign_id=campaign_id,
            target_authority=target_authority,
            scope_id=scope_id,
            attempt_id=attempt_id,
            action_id=action_id,
            candidate_id=candidate_id,
            experiment_id=experiment_id or attempt_id,
            action_kind=action_kind,
        )
        stored = await asyncio.to_thread(
            store.create_state_scope,
            campaign_id,
            target_authority,
            lifecycle._envelope(
                revision=1,
                before=state_scope_evidence(missing_reason="not_yet_observed"),
                action=state_scope_evidence(missing_reason="not_yet_observed"),
                responses=state_scope_evidence(missing_reason="not_yet_observed"),
                after=state_scope_evidence(missing_reason="not_yet_observed"),
            ),
            isolation_disposition="unknown",
        )
        expected_lane_revision = stored.get("expected_lane_revision")
        if isinstance(expected_lane_revision, int):
            lifecycle.lane_revision = expected_lane_revision
        return lifecycle

    @property
    def principal_key(self) -> str:
        identity = self.principal_identity
        if identity is None:
            return "anonymous"
        return _safe_identity_key(identity)

    def bind_safe_identities(
        self,
        *,
        principal_identity: Mapping[str, object] | None = None,
        physical_state_identity: Mapping[str, object] | None = None,
    ) -> None:
        if principal_identity is not None:
            self.principal_identity = dict(principal_identity)
        if physical_state_identity is not None:
            self.physical_state_identity = dict(physical_state_identity)

    def bind_lease(self, claim_id: str, claimant_id: str) -> None:
        self.claim_id = claim_id
        self.claimant_id = claimant_id

    def unbind_lease(self) -> None:
        self.claim_id = None
        self.claimant_id = None

    async def ensure_authoritative_before(self, evidence_refs: Sequence[str]) -> None:
        """Reconcile a restart-fenced lane before any new target effect."""

        reobserve = getattr(self.store, "reobserve_state_lane", None)
        if not callable(reobserve) or self.lane_revision is None:
            return
        claim_id, claimant_id = self._lease_binding()
        observed = await asyncio.to_thread(
            reobserve,
            self.scope_id,
            self.lane_revision,
            tuple(evidence_refs),
            claim_id=claim_id,
            claimant_id=claimant_id,
            principal_identity=self.principal_identity,
            physical_state_identity=self.physical_state_identity,
        )
        revision = observed.get("revision")
        if isinstance(revision, int):
            self.lane_revision = revision

    async def acknowledge(
        self,
        *,
        before_refs: Sequence[str],
        action_refs: Sequence[str],
    ) -> None:
        if self.acknowledged:
            return
        claim_id, claimant_id = self._lease_binding()
        next_revision = self.revision + 1
        await asyncio.to_thread(
            self.store.revise_state_scope,
            self.scope_id,
            self.revision,
            self._envelope(
                revision=next_revision,
                before=state_scope_evidence(before_refs),
                action=state_scope_evidence(action_refs),
                responses=state_scope_evidence(missing_reason="not_yet_observed"),
                after=state_scope_evidence(missing_reason="not_yet_observed"),
            ),
            claim_id=claim_id,
            claimant_id=claimant_id,
        )
        await asyncio.to_thread(
            self.store.acknowledge_state_scope,
            self.scope_id,
            claim_id=claim_id,
            claimant_id=claimant_id,
        )
        self.revision = next_revision
        self.acknowledged = True

    async def settle(
        self,
        *,
        before_refs: Sequence[str],
        action_refs: Sequence[str],
        response_refs: Sequence[str],
        after_refs: Sequence[str],
        response_missing_reason: str | None = "target_omitted",
        after_missing_reason: str | None = "selector_unavailable",
        transition_disposition: str,
        isolation_disposition: str,
        cleanup_disposition: str,
    ) -> None:
        if self.finished:
            return
        claim_id, claimant_id = self._lease_binding()
        next_revision = self.revision + 1
        await asyncio.to_thread(
            self.store.settle_state_scope,
            self.scope_id,
            self.revision,
            self._envelope(
                revision=next_revision,
                before=state_scope_evidence(before_refs),
                action=state_scope_evidence(action_refs),
                responses=(
                    state_scope_evidence(response_refs)
                    if response_refs
                    else state_scope_evidence(missing_reason=response_missing_reason)
                ),
                after=(
                    state_scope_evidence(after_refs)
                    if after_refs
                    else state_scope_evidence(missing_reason=after_missing_reason)
                ),
            ),
            transition_disposition=transition_disposition,
            isolation_disposition=isolation_disposition,
            cleanup_disposition=cleanup_disposition,
            claim_id=claim_id,
            claimant_id=claimant_id,
        )
        self.revision = next_revision
        self.finished = True

    async def pause(
        self,
        *,
        before_refs: Sequence[str] = (),
        action_refs: Sequence[str] = (),
        reason: str = "transport_failed",
        retryable: bool = True,
        isolation_disposition: str = "unknown",
        cleanup_disposition: str = "retained",
    ) -> None:
        if self.finished:
            return
        claim_id, claimant_id = self._lease_binding()
        next_revision = self.revision + 1
        envelope = self._envelope(
            revision=next_revision,
            before=(
                state_scope_evidence(before_refs)
                if before_refs
                else state_scope_evidence(missing_reason=reason, retryable=retryable)
            ),
            action=(
                state_scope_evidence(action_refs)
                if action_refs
                else state_scope_evidence(missing_reason=reason, retryable=retryable)
            ),
            responses=state_scope_evidence(missing_reason=reason, retryable=retryable),
            after=state_scope_evidence(missing_reason=reason, retryable=retryable),
        )
        if self.acknowledged:
            # Delivery already crossed the target boundary. It must be settled,
            # never sent through the pre-delivery pause transition (which the
            # store correctly rejects and would otherwise strand the scope).
            await asyncio.to_thread(
                self.store.settle_state_scope,
                self.scope_id,
                self.revision,
                envelope,
                isolation_disposition=isolation_disposition,
                cleanup_disposition=cleanup_disposition,
                transition_disposition="infrastructure_failure",
                claim_id=claim_id,
                claimant_id=claimant_id,
            )
        else:
            await asyncio.to_thread(
                self.store.pause_state_scope,
                self.scope_id,
                self.revision,
                envelope,
                isolation_disposition=isolation_disposition,
                cleanup_disposition=cleanup_disposition,
                transition_disposition="infrastructure_failure",
                claim_id=claim_id,
                claimant_id=claimant_id,
            )
        self.revision = next_revision
        self.finished = True

    async def pause_unclaimed(
        self,
        *,
        isolation_disposition: str = "unknown",
    ) -> None:
        if self.finished:
            return
        next_revision = self.revision + 1
        missing = state_scope_evidence(missing_reason="not_yet_observed", retryable=True)
        await asyncio.to_thread(
            self.store.pause_unclaimed_state_scope,
            self.scope_id,
            self.revision,
            self._envelope(
                revision=next_revision,
                before=missing,
                action=missing,
                responses=missing,
                after=missing,
            ),
            transition_disposition="infrastructure_failure",
            isolation_disposition=isolation_disposition,
            cleanup_disposition="retained",
        )
        self.revision = next_revision
        self.finished = True

    def _envelope(
        self,
        *,
        revision: int,
        before: Mapping[str, object],
        action: Mapping[str, object],
        responses: Mapping[str, object],
        after: Mapping[str, object],
    ) -> dict[str, object]:
        return {
            "schema_version": "merlin.state-scope.v1",
            "selector_contract_version": "merlin.state-selector.v1",
            "revision": revision,
            "scope_id": self.scope_id,
            "attempt_id": self.attempt_id,
            "action_id": self.action_id,
            "action_kind": self.action_kind.value,
            "campaign_id": self.campaign_id,
            "experiment_id": self.experiment_id,
            "candidate_id": self.candidate_id,
            "target_authority": self.target_authority,
            **({"isolation_lease_id": self.claim_id} if self.claim_id is not None else {}),
            **(
                {"principal_identity": dict(self.principal_identity)}
                if self.principal_identity is not None
                else {}
            ),
            **(
                {"physical_state_identity": dict(self.physical_state_identity)}
                if self.physical_state_identity is not None
                else {}
            ),
            "before": dict(before),
            "action": dict(action),
            "responses": dict(responses),
            "after": dict(after),
        }

    def _lease_binding(self) -> tuple[str, str]:
        if self.claim_id is None or self.claimant_id is None:
            raise RuntimeError("state scope mutation requires an active isolation lease")
        return self.claim_id, self.claimant_id


def _safe_identity_key(identity: Mapping[str, object]) -> str:
    kind = identity.get("kind")
    if kind == "keyed_digest":
        return ":".join(
            (
                "keyed_digest",
                str(identity.get("algorithm", "")),
                str(identity.get("key_id", "")),
                str(identity.get("digest", "")),
            )
        )
    if kind == "opaque_reference":
        return ":".join(
            (
                "opaque_reference",
                str(identity.get("namespace", "")),
                str(identity.get("reference_id", "")),
            )
        )
    raise ValueError("state-scope identity is not secret-safe")
