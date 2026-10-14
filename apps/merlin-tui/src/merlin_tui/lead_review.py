"""Identity and concurrency guards for Campaign Lead reviews.

Lead reviews may legitimately take minutes.  These helpers make that safe by
binding a review to an immutable input revision and by sharing an identical
in-flight inference instead of starting duplicate provider calls.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import TypeVar

_T = TypeVar("_T")


def _canonical_digest(value: object) -> str:
    body = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    return "sha256:" + hashlib.sha256(body.encode("utf-8")).hexdigest()


def authoritative_task_revision(tasks: Iterable[object]) -> str:
    """Digest scheduler-owned task state without trusting model-authored prose."""

    rows = []
    for task in tasks:
        outcomes = getattr(task, "technique_outcomes", {})
        rows.append(
            {
                "id": str(getattr(task, "id", "")),
                "status": str(getattr(task, "status", "")),
                "result_digest": _canonical_digest(str(getattr(task, "result", "") or "")),
                "technique_ids": tuple(
                    sorted(str(item) for item in (getattr(task, "technique_ids", ()) or ()))
                ),
                "technique_outcomes": tuple(
                    sorted(
                        (str(key), str(value))
                        for key, value in (
                            outcomes.items() if isinstance(outcomes, Mapping) else ()
                        )
                    )
                ),
            }
        )
    return _canonical_digest(sorted(rows, key=lambda row: row["id"]))


def guidance_revision(rows: Iterable[Mapping[str, object]]) -> str:
    """Digest only pending operator guidance, ignoring review journal chatter."""

    material = tuple(rows)
    consumed = {
        str(payload.get("guidance_id"))
        for row in material
        for payload in (row.get("payload"),)
        if isinstance(payload, Mapping) and payload.get("kind") == "operator_guidance_consumed"
    }
    pending = sorted(
        (
            str(payload.get("guidance_id")),
            _canonical_digest(str(payload.get("message") or "")),
        )
        for row in material
        for payload in (row.get("payload"),)
        if isinstance(payload, Mapping)
        and payload.get("kind") == "operator_guidance"
        and str(payload.get("guidance_id")) not in consumed
    )
    return _canonical_digest(pending)


def prompt_input_revision(
    task_snapshot: Mapping[str, object], rows: Iterable[Mapping[str, object]]
) -> str:
    """Digest the normalized task projection and journal inputs read by the prompt.

    The task snapshot includes scheduler tasks plus generated control-plane and
    worker-coordination tasks. Hashing only scheduler tasks can incorrectly
    coalesce reviews after evidence, findings, directives, or coordination
    messages change.
    """

    material = tuple(rows)
    prompt_journal = []
    for row in material:
        payload = row.get("payload")
        if not isinstance(payload, Mapping):
            continue
        if payload.get("kind") not in {
            "operator_guidance",
            "operator_guidance_consumed",
            "lead_decision_rejected",
            "lead_followup_dropped",
            "dynamic_specialist_rejected",
        }:
            continue
        prompt_journal.append(dict(payload))
    return _canonical_digest(
        {
            "task_snapshot": task_snapshot,
            "prompt_journal": prompt_journal,
        }
    )


@dataclass(frozen=True, slots=True)
class LeadReviewSnapshot:
    campaign_id: str
    profile: str
    attempt: int
    wave: int | None
    schedule_id: str | None
    task_revision: str
    guidance_revision: str
    prompt_revision: str
    blocking: bool
    waiting_for: str

    @property
    def revision(self) -> str:
        return _canonical_digest(
            {
                "campaign_id": self.campaign_id,
                "profile": self.profile,
                "attempt": self.attempt,
                "wave": self.wave,
                "schedule_id": self.schedule_id,
                "task_revision": self.task_revision,
                "guidance_revision": self.guidance_revision,
                "prompt_revision": self.prompt_revision,
                "blocking": self.blocking,
                "waiting_for": self.waiting_for,
            }
        )

    @property
    def review_id(self) -> str:
        return "lead-review:" + self.revision.removeprefix("sha256:")[:24]

    def projection(self) -> dict[str, object]:
        return {
            "review_id": self.review_id,
            "snapshot_revision": self.revision,
            "wave": self.wave,
            "schedule_id": self.schedule_id,
            "task_revision": self.task_revision,
            "guidance_revision": self.guidance_revision,
            "prompt_revision": self.prompt_revision,
            "blocking": self.blocking,
            "advisory": not self.blocking,
            "waiting_for": self.waiting_for,
        }


class LeadReviewCoalescer:
    """Share concurrent reviews of the exact same immutable snapshot."""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._in_flight: dict[str, asyncio.Task[object]] = {}
        self._latest_by_scope: dict[str, str] = {}

    async def run(
        self,
        revision: str,
        factory: Callable[[], Awaitable[_T]],
        *,
        scope: str | None = None,
    ) -> _T:
        async def discard(completed: asyncio.Task[object]) -> None:
            async with self._lock:
                if self._in_flight.get(revision) is completed:
                    self._in_flight.pop(revision, None)

        async with self._lock:
            if scope is not None:
                self._latest_by_scope[scope] = revision
            existing = self._in_flight.get(revision)
            if existing is None:
                task: asyncio.Task[object] = asyncio.create_task(factory())
                self._in_flight[revision] = task
                task.add_done_callback(lambda completed: asyncio.create_task(discard(completed)))
                existing = task
        try:
            # A cancelled observer must not cancel the shared provider inference.
            return await asyncio.shield(existing)  # type: ignore[return-value]
        finally:
            if existing.done():
                await discard(existing)

    async def is_current(self, scope: str, revision: str) -> bool:
        """Return whether no newer distinct snapshot superseded this review."""

        async with self._lock:
            return self._latest_by_scope.get(scope) == revision
