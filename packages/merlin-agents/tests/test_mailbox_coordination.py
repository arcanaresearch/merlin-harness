from datetime import UTC, datetime, timedelta

import pytest
from merlin_agents import (
    AttributedInput,
    DeliveryBoundary,
    DirectiveError,
    DirectiveKind,
    DurableMailbox,
    LeadCoordinator,
    LeadDirective,
    MailboxClaimError,
    WorkerCoordinationState,
)


class Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 1, 1, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now


def test_mail_is_sequenced_and_only_claimed_at_a_safe_boundary() -> None:
    clock = Clock()
    mailbox = DurableMailbox(clock=clock)
    first = mailbox.send(
        "worker",
        "operator",
        "redirect",
        safe_boundaries=[DeliveryBoundary.BEFORE_MODEL],
        message_id="one",
    )
    second = mailbox.send(
        "worker",
        "lead",
        "follow up",
        safe_boundaries=[DeliveryBoundary.AFTER_TOOL],
        message_id="two",
    )
    assert (first.sequence, second.sequence) == (1, 2)
    assert (
        mailbox.claim_next(
            "worker", "attempt-1", DeliveryBoundary.CHECKPOINT, lease_for=timedelta(seconds=5)
        )
        is None
    )
    claim = mailbox.claim_next(
        "worker", "attempt-1", DeliveryBoundary.BEFORE_MODEL, lease_for=timedelta(seconds=5)
    )
    assert claim is not None and claim.message.id == "one"
    assert (
        mailbox.claim_next(
            "worker", "attempt-2", DeliveryBoundary.BEFORE_MODEL, lease_for=timedelta(seconds=5)
        )
        is None
    )


def test_claim_renew_ack_and_stale_generation_rejection() -> None:
    clock = Clock()
    mailbox = DurableMailbox(clock=clock)
    mailbox.send(
        "worker",
        "lead",
        "bounded work",
        safe_boundaries=[DeliveryBoundary.CHECKPOINT],
    )
    old = mailbox.claim_next(
        "worker", "attempt-1", DeliveryBoundary.CHECKPOINT, lease_for=timedelta(seconds=2)
    )
    assert old is not None
    renewed = mailbox.renew(old, lease_for=timedelta(seconds=4))
    clock.now += timedelta(seconds=5)
    replacement = mailbox.claim_next(
        "worker", "attempt-2", DeliveryBoundary.CHECKPOINT, lease_for=timedelta(seconds=2)
    )
    assert replacement is not None and replacement.generation == old.generation + 1
    with pytest.raises(MailboxClaimError):
        mailbox.acknowledge(renewed)
    mailbox.acknowledge(replacement)
    assert mailbox.pending("worker") == ()


def test_snapshot_restores_sequence_and_acknowledgement() -> None:
    mailbox = DurableMailbox()
    mailbox.send("w", "lead", "one", safe_boundaries=[DeliveryBoundary.AFTER_MODEL], message_id="1")
    restored = DurableMailbox(mailbox.snapshot())
    message = restored.send(
        "w", "lead", "two", safe_boundaries=[DeliveryBoundary.AFTER_MODEL], message_id="2"
    )
    assert message.sequence == 2


def attributed(worker: str = "w1") -> AttributedInput:
    return AttributedInput(worker, 1, 1, "committed progress", evidence_ids=("e1",))


def directive(identifier: str, kind: DirectiveKind, **values: object) -> LeadDirective:
    arguments = {
        "id": identifier,
        "kind": kind,
        "lead_id": "lead",
        "worker_id": "w1",
        "reason": "new evidence",
        "source_input_ids": ("input-1",),
    }
    arguments.update(values)
    return LeadDirective(**arguments)  # type: ignore[arg-type]


def test_lead_uses_attributable_input_for_redirect_handoff_and_cancel() -> None:
    lead = LeadCoordinator("lead", [WorkerCoordinationState("w1"), WorkerCoordinationState("w2")])
    lead.consume("input-1", attributed())
    lead.issue(directive("redirect", DirectiveKind.REDIRECT, objective="test refresh flow"))
    lead.issue(directive("handoff", DirectiveKind.HANDOFF, handoff_worker_id="w2"))
    assert lead.worker_state("w1").active is False
    with pytest.raises(DirectiveError):
        lead.issue(directive("cancel", DirectiveKind.CANCEL))

    cancelling = LeadCoordinator("lead", [WorkerCoordinationState("w1")])
    cancelling.consume("input-1", attributed())
    cancelling.issue(directive("cancel", DirectiveKind.CANCEL))
    assert cancelling.worker_state("w1").active is False


def test_followups_are_bounded_and_unattributed_directives_rejected() -> None:
    lead = LeadCoordinator("lead", [WorkerCoordinationState("w1")], max_followups_per_worker=1)
    lead.consume("input-1", attributed())
    lead.issue(directive("f1", DirectiveKind.FOLLOW_UP, objective="verify once"))
    with pytest.raises(DirectiveError, match="bound exhausted"):
        lead.issue(directive("f2", DirectiveKind.FOLLOW_UP, objective="verify twice"))
    with pytest.raises(DirectiveError, match="unknown or unattributed"):
        lead.issue(
            directive(
                "bad",
                DirectiveKind.REDIRECT,
                objective="guess",
                source_input_ids=("missing",),
            )
        )
    with pytest.raises(ValueError, match="committed"):
        AttributedInput("w1", 1, 1, "uncommitted", committed=False)
