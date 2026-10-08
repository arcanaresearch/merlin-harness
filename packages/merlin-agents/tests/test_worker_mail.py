"""Phase 1 typed mailbox: addressing, delivery modes, settlement, bounded delta."""

from types import SimpleNamespace

import pytest
from merlin_agents import (
    DeliveryMode,
    WorkerControl,
    WorkerMail,
    WorkerSettlement,
)
from merlin_agents.durable_service import DurableAgentService
from merlin_agents.mailbox import (
    MAIL_BODY_MAX_CHARS,
    MailboxError,
    compact_mail_batch,
    estimate_tokens,
    render_mail,
    render_settlement,
)


def test_worker_mail_envelope_preserves_provenance() -> None:
    mail = WorkerMail(
        sender_id="campaign/1/recon",
        body="Session cookie is scoped to /admin.",
        mode=DeliveryMode.STEER,
        campaign_id="campaign/1",
        task_id="campaign/1/attack",
        evidence_refs=("sha256:abc", "merlin-result://recon/1"),
    )

    rendered = render_mail(mail)

    assert rendered.startswith("Worker mail from campaign/1/recon (")
    assert "campaign=campaign/1" in rendered
    assert "task=campaign/1/attack" in rendered
    assert "mode=steer" in rendered
    assert "sha256:abc" in rendered
    assert "Operator message" not in rendered


def test_worker_mail_rejects_empty_and_oversized_bodies() -> None:
    with pytest.raises(MailboxError):
        WorkerMail(sender_id="", body="hello")
    with pytest.raises(MailboxError):
        WorkerMail(sender_id="sender", body="x" * (MAIL_BODY_MAX_CHARS + 1))
    bounded = WorkerMail.bounded("sender", "x" * (MAIL_BODY_MAX_CHARS + 500))
    assert len(bounded.body) <= MAIL_BODY_MAX_CHARS
    assert bounded.body.endswith("full body remains in the durable mailbox store]")


def test_worker_control_separates_wake_and_quiet_modes() -> None:
    control = WorkerControl()
    control.send("follow up now", message_id="m1", mode=DeliveryMode.FOLLOW_UP)
    control.send("steer at boundary", message_id="m2", mode=DeliveryMode.STEER)
    control.send("context only", message_id="m3", mode=DeliveryMode.INJECT)

    wake = control.drain(budget_tokens=1_024)
    assert [(message_id, mail.mode) for message_id, mail in wake] == [
        ("m1", DeliveryMode.FOLLOW_UP),
        ("m2", DeliveryMode.STEER),
    ]
    quiet = control.drain_context(budget_tokens=1_024)
    assert [(message_id, mail.mode) for message_id, mail in quiet] == [("m3", DeliveryMode.INJECT)]
    assert control.take_consumed_ids() == ("m1", "m2", "m3")


def test_worker_control_inject_mail_is_not_returned_by_wake_drain() -> None:
    control = WorkerControl()
    control.send("context only", message_id="m1", mode=DeliveryMode.INJECT)

    assert control.drain(budget_tokens=1_024) == ()
    # Inject mail stays queued until a context drain; it is not lost.
    quiet = control.drain_context(budget_tokens=1_024)
    assert [message_id for message_id, _ in quiet] == ["m1"]


def test_worker_control_fails_closed_when_budget_unknown() -> None:
    control = WorkerControl()
    control.send("wake me", message_id="m1", mode=DeliveryMode.FOLLOW_UP)
    control.send("quiet", message_id="m2", mode=DeliveryMode.INJECT)

    assert control.drain() == ()
    assert control.drain_context() == ()
    assert control.take_consumed_ids() == ()
    # Nothing was consumed: with a known budget the same mail is delivered.
    assert [message_id for message_id, _ in control.drain(budget_tokens=1_024)] == ["m1"]
    assert [message_id for message_id, _ in control.drain_context(budget_tokens=1_024)] == ["m2"]


def test_overflowing_batch_compacts_to_summary_and_refs() -> None:
    mails = tuple(
        WorkerMail(
            sender_id=f"worker:{index}",
            body="y" * 400,
            task_id=f"campaign/1/task-{index}",
            evidence_refs=(f"sha256:{index:064x}",),
        )
        for index in range(4)
    )

    kept, deferred = compact_mail_batch(mails, budget_tokens=estimate_tokens("y" * 400) * 2 + 8)

    assert not deferred
    assert len(kept) < len(mails) + 1
    summary = kept[-1]
    assert summary.sender_id == "mailbox-compaction"
    assert "compacted to references" in summary.body
    assert "refs=" in summary.body
    # The compacted batch is a bounded delta within the budget.
    assert sum(estimate_tokens(render_mail(mail)) for mail in kept) <= (
        estimate_tokens("y" * 400) * 2 + 8 + estimate_tokens(summary.body)
    )


def test_compact_mail_batch_fails_closed_without_budget() -> None:
    mails = (WorkerMail(sender_id="worker:1", body="hello"),)
    kept, deferred = compact_mail_batch(mails, budget_tokens=None)
    assert kept == ()
    assert deferred


def test_drain_acknowledges_overflowing_messages_via_compaction() -> None:
    control = WorkerControl()
    for index in range(4):
        control.send(
            f"z{index}" * 200,
            message_id=f"m{index}",
            sender=f"worker:{index}",
        )

    drained = control.drain(budget_tokens=estimate_tokens("z" * 400) + 32)

    assert drained
    assert drained[-1][1].sender_id == "mailbox-compaction"
    # Every durable delivery is acknowledged even when compacted.
    assert control.take_consumed_ids() == ("m0", "m1", "m2", "m3")


def test_worker_settlement_is_typed_and_reference_only() -> None:
    settlement = WorkerSettlement(
        task_id="campaign/1/attack",
        status="completed",
        result_digest="sha256:deadbeef",
        summary="Confirmed reflected XSS at /search.",
        evidence_refs=("sha256:deadbeef",),
    )

    rendered = render_settlement(settlement)

    assert "task campaign/1/attack terminated with status completed" in rendered
    assert "sha256:deadbeef" in rendered
    assert "fetch the full result from the result store" in rendered

    mail = settlement.to_mail(sender_id="worker:campaign/1/attack")
    assert mail.schema_id == "merlin.worker-settlement.v1"
    assert mail.task_id == "campaign/1/attack"
    assert mail.evidence_refs == ("sha256:deadbeef",)

    with pytest.raises(MailboxError):
        WorkerSettlement(task_id="t", status="", result_digest="d")


class _MessageStore:
    """Minimal scheduler-store fake capturing durable mailbox writes."""

    def __init__(self) -> None:
        self.sent = []
        self.claims = []

    def send_message_once(self, message_id, schedule_id, sender, recipient, schema_id, payload):
        row = SimpleNamespace(
            id=message_id,
            schedule_id=schedule_id,
            sender=sender,
            recipient=recipient,
            payload=payload,
        )
        self.sent.append(row)
        return row, True

    def claim_message(self, attempt_id, generation, recipient, message_id):
        self.claims.append(message_id)
        return SimpleNamespace(id=message_id)


class _Provider:
    capabilities = None


class _Tools:
    def describe(self):
        return ()


async def _service_with_live(store: _MessageStore):
    from merlin_agents.durable_service import _LiveSchedule  # noqa: PLC0415
    from merlin_agents.scheduler import NodeSpec, ReferenceScheduler  # noqa: PLC0415

    service = DurableAgentService(_Provider(), _Tools(), store)  # type: ignore[arg-type]
    control = WorkerControl()
    live = _LiveSchedule(
        id="schedule-1",
        session_id="session-1",
        graph=SimpleNamespace(spec=SimpleNamespace(id="swarm:session-1")),
        assignments=(),
        scheduler=ReferenceScheduler([NodeSpec("worker:a")], []),
        controls={"worker:a": control},
        worker_contexts={},
        attempts={"worker:a": SimpleNamespace(id="attempt-1", generation=1)},
    )
    service._live["schedule-1"] = live  # noqa: SLF001
    service._session_active["session-1"] = "schedule-1"  # noqa: SLF001
    return service, live, control


async def test_follow_up_delivery_wakes_the_live_worker() -> None:
    store = _MessageStore()
    service, live, control = await _service_with_live(store)

    created = await service.message_agent_once(
        "session-1",
        "worker:a",
        "reassess the frontier",
        message_id="msg-1",
        sender_id="worker:b",
        campaign_id="campaign/1",
        task_id="campaign/1/attack",
        evidence_refs=("sha256:abc",),
    )

    assert created
    assert store.claims == ["msg-1"]
    assert store.sent[0].payload["mode"] == "follow_up"
    assert store.sent[0].payload["sender"] == "worker:b"
    drained = control.drain(budget_tokens=1_024)
    assert len(drained) == 1
    _, mail = drained[0]
    assert mail.sender_id == "worker:b"
    assert mail.campaign_id == "campaign/1"
    assert mail.task_id == "campaign/1/attack"
    assert mail.evidence_refs == ("sha256:abc",)
    assert mail.mode is DeliveryMode.FOLLOW_UP


async def test_inject_delivery_is_durable_but_never_wakes() -> None:
    store = _MessageStore()
    service, live, control = await _service_with_live(store)

    created = await service.message_agent_once(
        "session-1",
        "worker:a",
        "context only note",
        message_id="msg-2",
        mode=DeliveryMode.INJECT,
        sender_id="worker:b",
    )

    assert created
    # Durable row queued, but no live claim: the worker is not woken.
    assert [row.id for row in store.sent] == ["msg-2"]
    assert store.claims == []
    assert control.drain(budget_tokens=1_024) == ()
    assert control.drain_context(budget_tokens=1_024) == ()


async def test_settlement_notice_delivered_to_child_owner() -> None:
    store = _MessageStore()
    service, live, _ = await _service_with_live(store)
    child_live = SimpleNamespace(
        id="child-1",
        session_id="session-1",
        graph=SimpleNamespace(spec=SimpleNamespace(id="child:schedule-1:worker:a")),
    )
    service._live["child-1"] = child_live  # noqa: SLF001
    assignment = SimpleNamespace(id="child-1/probe")
    result = SimpleNamespace(error=None, content='{"finding": "x"}')

    await service._deliver_settlement(  # noqa: SLF001
        child_live,
        assignment,
        result,
        "sha256:cafe",
        outcome="completed",
        attempt_id="attempt-child",
    )

    assert [row.id for row in store.sent] == ["settlement:attempt-child"]
    row = store.sent[0]
    assert row.schedule_id == "schedule-1"
    assert row.recipient == "worker:a"
    assert row.payload["mode"] == "follow_up"
    assert row.payload["sender"] == "worker:child-1/probe"
    assert row.payload["task_id"] == "child-1/probe"
    assert row.payload["evidence_refs"] == ["sha256:cafe"]
    assert (
        "Worker settlement: task child-1/probe terminated with status completed."
        in (row.payload["message"])
    )


async def test_settlement_skipped_for_top_level_wave_workers() -> None:
    store = _MessageStore()
    service, live, _ = await _service_with_live(store)

    await service._deliver_settlement(  # noqa: SLF001
        live,
        SimpleNamespace(id="campaign/1/attack"),
        SimpleNamespace(error=None, content="{}"),
        "sha256:cafe",
        outcome="completed",
        attempt_id="attempt-1",
    )

    assert store.sent == []
