import asyncio
from types import SimpleNamespace

import pytest
from merlin_tui.lead_review import (
    LeadReviewCoalescer,
    LeadReviewSnapshot,
    authoritative_task_revision,
    guidance_revision,
    prompt_input_revision,
)


def test_authoritative_task_revision_changes_with_authoritative_state() -> None:
    completed = SimpleNamespace(
        id="task-1",
        status="completed",
        result="receipt",
        technique_ids=("technique-1",),
        technique_outcomes={"technique-1": "refuted"},
    )
    running = SimpleNamespace(
        id="task-1",
        status="running",
        result="",
        technique_ids=("technique-1",),
        technique_outcomes={},
    )

    assert authoritative_task_revision((completed,)) == authoritative_task_revision((completed,))
    assert authoritative_task_revision((completed,)) != authoritative_task_revision((running,))


def test_guidance_revision_ignores_review_chatter_and_tracks_pending_guidance() -> None:
    guidance = {
        "payload": {
            "kind": "operator_guidance",
            "guidance_id": "guidance-1",
            "message": "Prioritize the remaining objective",
        }
    }
    review = {"payload": {"kind": "lead_review_progress", "completed_chunks": 8}}
    consumed = {
        "payload": {
            "kind": "operator_guidance_consumed",
            "guidance_id": "guidance-1",
        }
    }

    assert guidance_revision((guidance,)) == guidance_revision((guidance, review))
    assert guidance_revision((guidance,)) != guidance_revision((guidance, consumed))


def test_prompt_revision_covers_control_and_coordination_inputs() -> None:
    base = {
        "campaign_id": "campaign-1",
        "profile": "recommended",
        "attempt": 0,
        "tasks": [
            {
                "id": "campaign-1/lead-control/evidence_get",
                "status": "completed",
                "result": {"evidence": ["receipt-1"]},
            },
            {
                "id": "campaign-1/worker-message/message-1",
                "status": "completed",
                "result": "worker coordination A",
            },
        ],
    }
    evidence_changed = {
        **base,
        "tasks": [
            {**base["tasks"][0], "result": {"evidence": ["receipt-2"]}},
            base["tasks"][1],
        ],
    }
    coordination_changed = {
        **base,
        "tasks": [
            base["tasks"][0],
            {**base["tasks"][1], "result": "worker coordination B"},
        ],
    }

    revision = prompt_input_revision(base, ())
    assert revision != prompt_input_revision(evidence_changed, ())
    assert revision != prompt_input_revision(coordination_changed, ())


def test_lead_review_snapshot_identity_binds_schedule_tasks_and_guidance() -> None:
    snapshot = LeadReviewSnapshot(
        campaign_id="campaign-1",
        profile="recommended",
        attempt=0,
        wave=2,
        schedule_id="schedule-1",
        task_revision="sha256:tasks",
        guidance_revision="sha256:guidance",
        prompt_revision="sha256:prompt",
        blocking=True,
        waiting_for="Lead decision before the next wave can be scheduled",
    )

    assert snapshot.review_id.startswith("lead-review:")
    assert snapshot.projection()["snapshot_revision"] == snapshot.revision


@pytest.mark.asyncio
async def test_lead_review_coalescer_shares_only_concurrent_identical_reviews() -> None:
    coalescer = LeadReviewCoalescer()
    calls = 0
    gate = asyncio.Event()

    async def review() -> str:
        nonlocal calls
        calls += 1
        await gate.wait()
        return "decision"

    first = asyncio.create_task(coalescer.run("revision-1", review))
    second = asyncio.create_task(coalescer.run("revision-1", review))
    for _ in range(10):
        await asyncio.sleep(0)
        if calls:
            break
    assert calls == 1
    gate.set()
    assert await asyncio.gather(first, second) == ["decision", "decision"]

    assert await coalescer.run("revision-1", review) == "decision"
    assert calls == 2


@pytest.mark.asyncio
async def test_lead_review_coalescer_does_not_share_distinct_revisions() -> None:
    coalescer = LeadReviewCoalescer()
    calls = 0

    async def review() -> int:
        nonlocal calls
        calls += 1
        await asyncio.sleep(0)
        return calls

    await asyncio.gather(
        coalescer.run("revision-1", review),
        coalescer.run("revision-2", review),
    )

    assert calls == 2


@pytest.mark.asyncio
async def test_new_prompt_revision_supersedes_older_review_in_same_campaign() -> None:
    coalescer = LeadReviewCoalescer()
    first_started = asyncio.Event()
    release_first = asyncio.Event()

    async def first_review() -> str:
        first_started.set()
        await release_first.wait()
        return "old"

    async def second_review() -> str:
        return "new"

    first = asyncio.create_task(
        coalescer.run("prompt-a", first_review, scope="campaign-1")
    )
    await first_started.wait()
    assert await coalescer.run("prompt-b", second_review, scope="campaign-1") == "new"
    assert not await coalescer.is_current("campaign-1", "prompt-a")
    assert await coalescer.is_current("campaign-1", "prompt-b")
    release_first.set()
    assert await first == "old"
