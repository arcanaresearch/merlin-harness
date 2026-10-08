from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from uuid import uuid4


class MailboxError(ValueError):
    pass


class MailboxClaimError(MailboxError):
    pass


class DeliveryMode(StrEnum):
    """How a worker-mail envelope reaches its recipient.

    FOLLOW_UP: enqueue durably and wake the live worker for immediate delivery.
    STEER: wake the live worker and deliver at its next step boundary.
    INJECT: context-only — durable, delivered with the next natural turn, never
    wakes the worker and never forces a turn of its own.
    """

    FOLLOW_UP = "follow_up"
    STEER = "steer"
    INJECT = "inject"


WAKE_MODES = frozenset({DeliveryMode.FOLLOW_UP, DeliveryMode.STEER})

MAIL_BODY_MAX_CHARS = 4_096
_MAIL_SINGLE_MAX_TOKENS = 2_048

# Heuristic chars-per-token upper bound used to budget mail batches against the
# resolved model context window without a provider tokenizer in the loop.
CHARS_PER_TOKEN = 4


def estimate_tokens(text: str) -> int:
    return max(1, (len(text) + CHARS_PER_TOKEN - 1) // CHARS_PER_TOKEN)


@dataclass(frozen=True, slots=True)
class WorkerMail:
    """Typed worker-mail envelope.

    Carries provenance (sender, campaign, task), evidence *references*, and the
    delivery mode end-to-end. Bodies are bounded coordination text; evidence
    stays in the store and is fetched on demand — never inline payloads.
    """

    sender_id: str
    body: str
    mode: DeliveryMode = DeliveryMode.FOLLOW_UP
    campaign_id: str | None = None
    task_id: str | None = None
    evidence_refs: tuple[str, ...] = ()
    schema_id: str = "merlin.worker-mail.v1"

    def __post_init__(self) -> None:
        if not self.sender_id.strip() or not self.body.strip():
            raise MailboxError("worker mail requires a sender and a body")
        if len(self.body) > MAIL_BODY_MAX_CHARS:
            raise MailboxError("worker mail body exceeds its bound")
        object.__setattr__(self, "evidence_refs", tuple(self.evidence_refs))
        if any(not ref.strip() or "\n" in ref for ref in self.evidence_refs):
            raise MailboxError("evidence references must be single-line store references")
        if estimate_tokens(self.body) > _MAIL_SINGLE_MAX_TOKENS:
            raise MailboxError("worker mail body exceeds the single-message token bound")

    @classmethod
    def bounded(cls, sender_id: str, body: str, **kwargs: object) -> WorkerMail:
        """Build an envelope, compacting an over-long body to a bounded prefix.

        Durable payloads written before the typed envelope (or long recon
        handoffs) can exceed the mail body bound. The store row stays
        authoritative; the delivered envelope is compacted to a bounded prefix
        plus a reference, never an inlined replay.
        """
        try:
            return cls(sender_id=sender_id, body=body, **kwargs)  # type: ignore[arg-type]
        except MailboxError:
            marker = "\n[compacted: full body remains in the durable mailbox store]"
            truncated = body[: MAIL_BODY_MAX_CHARS - len(marker)] + marker
            return cls(sender_id=sender_id, body=truncated, **kwargs)  # type: ignore[arg-type]


@dataclass(frozen=True, slots=True)
class WorkerSettlement:
    """Typed terminal-result notice delivered to a worker's owning lead/specialist.

    References, not payloads: the result body stays in the result store; the
    notice carries the digest and evidence refs so the owner fetches on demand.
    """

    task_id: str
    status: str
    result_digest: str
    summary: str = ""
    campaign_id: str | None = None
    evidence_refs: tuple[str, ...] = ()
    schema_id: str = "merlin.worker-settlement.v1"

    def __post_init__(self) -> None:
        if not self.task_id.strip() or not self.status.strip() or not self.result_digest.strip():
            raise MailboxError("a settlement requires a task, a status, and a result digest")
        if len(self.summary) > MAIL_BODY_MAX_CHARS:
            raise MailboxError("settlement summary exceeds its bound")
        object.__setattr__(self, "evidence_refs", tuple(self.evidence_refs))
        if any(not ref.strip() or "\n" in ref for ref in self.evidence_refs):
            raise MailboxError("evidence references must be single-line store references")

    def to_mail(self, *, sender_id: str, mode: DeliveryMode = DeliveryMode.INJECT) -> WorkerMail:
        return WorkerMail(
            sender_id=sender_id,
            body=render_settlement(self),
            mode=mode,
            campaign_id=self.campaign_id,
            task_id=self.task_id,
            evidence_refs=self.evidence_refs,
            schema_id=self.schema_id,
        )


def render_mail(mail: WorkerMail) -> str:
    """Render an envelope for the model context with provenance intact."""
    origin = f"Worker mail from {mail.sender_id}"
    provenance = [
        f"campaign={mail.campaign_id}" if mail.campaign_id else "",
        f"task={mail.task_id}" if mail.task_id else "",
        f"mode={mail.mode.value}",
    ]
    header = origin + " (" + ", ".join(item for item in provenance if item) + ")"
    refs = ""
    if mail.evidence_refs:
        refs = (
            "\nEvidence references (fetch from the evidence store on demand; "
            "not inlined): " + ", ".join(mail.evidence_refs)
        )
    return f"{header}:\n{mail.body}{refs}"


def render_settlement(settlement: WorkerSettlement) -> str:
    lines = [
        f"Worker settlement: task {settlement.task_id} terminated with status {settlement.status}.",
        f"Result digest: {settlement.result_digest} "
        "(fetch the full result from the result store on demand; not inlined).",
    ]
    if settlement.evidence_refs:
        lines.append("Evidence references: " + ", ".join(settlement.evidence_refs))
    if settlement.summary:
        lines.append(f"Summary: {settlement.summary}")
    return "\n".join(lines)


def compact_mail_batch(
    mails: Sequence[WorkerMail],
    *,
    budget_tokens: int | None,
) -> tuple[tuple[WorkerMail, ...], bool]:
    """Bound a mail batch to ``budget_tokens`` as a bounded delta, never a replay.

    Returns the mails to inject (whole envelopes in arrival order, then a
    summary + refs envelope compacting any overflow) and whether injection was
    deferred. Fails closed: when the token budget is unknown (None), nothing is
    injected and the batch must be deferred and journaled by the caller.
    """
    values = tuple(mails)
    if not values:
        return (), False
    if budget_tokens is None:
        return (), True
    kept: list[WorkerMail] = []
    used = 0
    overflow: list[WorkerMail] = []
    for mail in values:
        cost = estimate_tokens(render_mail(mail))
        if kept and used + cost > budget_tokens:
            overflow.append(mail)
            continue
        if not kept and cost > budget_tokens:
            overflow.append(mail)
            continue
        kept.append(mail)
        used += cost
    if overflow:
        descriptors = [
            f"{mail.sender_id}"
            + (f" task={mail.task_id}" if mail.task_id else "")
            + (f" refs={','.join(mail.evidence_refs)}" if mail.evidence_refs else " refs=none")
            for mail in overflow
        ]
        summary = (
            f"{len(overflow)} additional worker message(s) exceeded the injection "
            "budget and were compacted to references; fetch bodies on demand:\n"
            + "\n".join(descriptors)
        )
        if estimate_tokens(summary) > budget_tokens:
            summary = summary[: budget_tokens * CHARS_PER_TOKEN]
        kept.append(
            WorkerMail(
                sender_id="mailbox-compaction",
                body=summary,
                mode=values[0].mode,
                campaign_id=values[0].campaign_id,
            )
        )
    return tuple(kept), False


class DeliveryBoundary(StrEnum):
    BEFORE_MODEL = "before_model"
    AFTER_MODEL = "after_model"
    AFTER_TOOL = "after_tool"
    CHECKPOINT = "checkpoint"


@dataclass(frozen=True, slots=True)
class AttributedInput:
    source_agent_id: str
    source_attempt: int
    source_generation: int
    content: str
    committed: bool = True
    evidence_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.source_agent_id.strip() or not self.content.strip():
            raise ValueError("attributed input requires a source agent and content")
        if self.source_attempt < 1 or self.source_generation < 1:
            raise ValueError("attempt and generation must be positive")
        if not self.committed:
            raise ValueError("lead input must already be committed and attributable")
        object.__setattr__(self, "evidence_ids", tuple(self.evidence_ids))


@dataclass(frozen=True, slots=True)
class MailMessage:
    id: str
    recipient_id: str
    sender_id: str
    sequence: int
    body: str
    created_at: datetime
    safe_boundaries: frozenset[DeliveryBoundary]
    attribution: AttributedInput | None = None
    claim_id: str | None = None
    claimed_by: str | None = None
    claim_generation: int = 0
    claim_expires_at: datetime | None = None
    acknowledged_at: datetime | None = None

    @property
    def acknowledged(self) -> bool:
        return self.acknowledged_at is not None


@dataclass(frozen=True, slots=True)
class MailClaim:
    message: MailMessage
    claim_id: str
    generation: int
    expires_at: datetime


Clock = Callable[[], datetime]


class DurableMailbox:
    """Persistence-ready deterministic reducer for sequenced mailbox records."""

    def __init__(self, records: Iterable[MailMessage] = (), *, clock: Clock | None = None) -> None:
        self._clock = clock or (lambda: datetime.now(UTC))
        self._messages: dict[str, MailMessage] = {}
        self._next_sequence: dict[str, int] = {}
        for record in records:
            if record.id in self._messages:
                raise MailboxError(f"duplicate mailbox message {record.id!r}")
            if record.sequence < 1 or record.created_at.tzinfo is None:
                raise MailboxError("persisted mailbox record is invalid")
            self._messages[record.id] = record
            self._next_sequence[record.recipient_id] = max(
                self._next_sequence.get(record.recipient_id, 1), record.sequence + 1
            )

    def send(
        self,
        recipient_id: str,
        sender_id: str,
        body: str,
        *,
        safe_boundaries: Iterable[DeliveryBoundary],
        attribution: AttributedInput | None = None,
        message_id: str | None = None,
    ) -> MailMessage:
        boundaries = frozenset(safe_boundaries)
        if not recipient_id.strip() or not sender_id.strip() or not body.strip():
            raise MailboxError("recipient, sender, and body must not be empty")
        if not boundaries:
            raise MailboxError("a message must declare at least one safe delivery boundary")
        identifier = message_id or uuid4().hex
        if identifier in self._messages:
            raise MailboxError(f"duplicate mailbox message {identifier!r}")
        sequence = self._next_sequence.get(recipient_id, 1)
        message = MailMessage(
            identifier,
            recipient_id,
            sender_id,
            sequence,
            body,
            self._now(),
            boundaries,
            attribution,
        )
        self._messages[identifier] = message
        self._next_sequence[recipient_id] = sequence + 1
        return message

    def claim_next(
        self,
        recipient_id: str,
        claimant_id: str,
        boundary: DeliveryBoundary,
        *,
        lease_for: timedelta,
    ) -> MailClaim | None:
        if not recipient_id.strip() or not claimant_id.strip():
            raise MailboxClaimError("recipient and claimant must not be empty")
        if lease_for <= timedelta(0):
            raise MailboxClaimError("mail claim lease must be positive")
        now = self._now()
        candidates = sorted(
            (
                message
                for message in self._messages.values()
                if message.recipient_id == recipient_id
                and not message.acknowledged
                and boundary in message.safe_boundaries
                and (message.claim_expires_at is None or message.claim_expires_at <= now)
            ),
            key=lambda message: message.sequence,
        )
        if not candidates:
            return None
        message = candidates[0]
        claim_id = uuid4().hex
        expires_at = now + lease_for
        claimed = replace(
            message,
            claim_id=claim_id,
            claimed_by=claimant_id,
            claim_generation=message.claim_generation + 1,
            claim_expires_at=expires_at,
        )
        self._messages[message.id] = claimed
        return MailClaim(claimed, claim_id, claimed.claim_generation, expires_at)

    def renew(self, claim: MailClaim, *, lease_for: timedelta) -> MailClaim:
        if lease_for <= timedelta(0):
            raise MailboxClaimError("mail claim lease must be positive")
        message = self._validate_claim(claim)
        now = self._now()
        if message.claim_expires_at is None or message.claim_expires_at <= now:
            raise MailboxClaimError("mail claim has expired")
        renewed = replace(message, claim_expires_at=now + lease_for)
        self._messages[message.id] = renewed
        return MailClaim(renewed, claim.claim_id, claim.generation, renewed.claim_expires_at)

    def acknowledge(self, claim: MailClaim) -> MailMessage:
        message = self._validate_claim(claim)
        now = self._now()
        if message.claim_expires_at is None or message.claim_expires_at <= now:
            raise MailboxClaimError("mail claim has expired")
        acknowledged = replace(message, acknowledged_at=now)
        self._messages[message.id] = acknowledged
        return acknowledged

    def pending(self, recipient_id: str) -> tuple[MailMessage, ...]:
        return tuple(
            sorted(
                (
                    message
                    for message in self._messages.values()
                    if message.recipient_id == recipient_id and not message.acknowledged
                ),
                key=lambda message: message.sequence,
            )
        )

    def snapshot(self) -> tuple[MailMessage, ...]:
        return tuple(
            sorted(self._messages.values(), key=lambda item: (item.recipient_id, item.sequence))
        )

    def _validate_claim(self, claim: MailClaim) -> MailMessage:
        try:
            message = self._messages[claim.message.id]
        except KeyError as error:
            raise MailboxClaimError("unknown mailbox message") from error
        if (
            message.claim_id != claim.claim_id
            or message.claim_generation != claim.generation
            or message.acknowledged
        ):
            raise MailboxClaimError("stale or invalid mailbox claim")
        return message

    def _now(self) -> datetime:
        now = self._clock()
        if now.tzinfo is None:
            raise MailboxError("mailbox clock must return a timezone-aware datetime")
        return now
