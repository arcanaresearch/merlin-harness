import asyncio
from types import SimpleNamespace
from uuid import uuid4

from merlin_agents import AssignmentBudget, DurableAgentService, WorkerAssignment
from merlin_agents.durable_service import (
    _AdaptiveConcurrency,
    _is_rate_limit_failure,
)
from merlin_agents.runtime import WorkerResult
from merlin_contracts import ModelCompleted, ModelError, TextDelta


# Self-contained fakes, mirroring test_durable_service.py (sibling test imports
# are unsupported under the repo's --import-mode=importlib pytest config).
class Provider:
    async def stream(self, request):
        yield TextDelta(text='{"finding":"checked"}')
        yield ModelCompleted()


class Tools:
    def describe(self):
        return ()

    async def invoke(self, name, context, arguments):
        raise AssertionError("no tools admitted")


class Store:
    def __init__(self):
        self.schedules = {}
        self.nodes = {}
        self.results = {}
        self.attempts = {}
        self.created_before_claim = False

    def create_schedule(
        self,
        session_id,
        graph,
        checkpoint,
        node_ids,
        *,
        ready_nodes=(),
        schedule_id=None,
        initial_status="running",
        parent_schedule_id=None,
        parent_attempt_id=None,
    ):
        identifier = schedule_id or "schedule-1"
        self.schedules[identifier] = {
            "id": identifier,
            "session_id": session_id,
            "graph": graph,
            "checkpoint": checkpoint,
            "parent_schedule_id": parent_schedule_id,
            "parent_attempt_id": parent_attempt_id,
            "event_count": 1,
            "status": initial_status,
        }
        self.nodes[identifier] = {
            node: {"node_id": node, "state": "ready" if node in ready_nodes else "pending"}
            for node in node_ids
        }
        return identifier

    def resume_schedule(self, schedule_id):
        self.schedules[schedule_id]["status"] = "running"

    def load_schedule(self, schedule_id):
        return self.schedules.get(schedule_id)

    def list_schedules(self, session_id, *, limit=50):
        return [row for row in self.schedules.values() if row["session_id"] == session_id][:limit]

    def list_active_schedules(self, *, limit=50):
        return [
            row
            for identifier, row in self.schedules.items()
            if row["status"] in {"running", "paused"}
            or (
                row["status"] == "cancelled"
                and any(
                    node_id.startswith("cleanup:") and node["state"] in {"ready", "leased"}
                    for node_id, node in self.nodes[identifier].items()
                )
            )
        ][:limit]

    def find_active_task(self, task_id):
        node_id = task_id if task_id.startswith("worker:") else f"worker:{task_id}"
        return [
            {**schedule, "node_id": node_id}
            for schedule_id, schedule in self.schedules.items()
            if schedule["status"] == "running"
            and node_id in self.nodes[schedule_id]
            and self.nodes[schedule_id][node_id]["state"]
            not in {"completed", "failed", "cancelled"}
        ]

    def list_nodes(self, schedule_id):
        return list(self.nodes[schedule_id].values())

    def list_attempts(self, schedule_id):
        return [
            vars(attempt).copy()
            for attempt in self.attempts.values()
            if attempt.schedule_id == schedule_id
        ]

    def list_results(self, schedule_id):
        return list(self.results.get(schedule_id, {}).values())

    def restore(self, schedule_id):
        return {
            "schedule": self.schedules[schedule_id],
            "nodes": self.list_nodes(schedule_id),
            "attempts": self.list_attempts(schedule_id),
            "results": self.list_results(schedule_id),
            "events": [],
        }

    def transition_ready(self, schedule_id, node_ids, *, checkpoint, expected_event_count, **_):
        assert self.schedules[schedule_id]["event_count"] == expected_event_count
        for node in node_ids:
            self.nodes[schedule_id][node]["state"] = "ready"
        self._advance(schedule_id, checkpoint)

    def transition_failed(self, schedule_id, failures, *, checkpoint, expected_event_count):
        assert self.schedules[schedule_id]["event_count"] == expected_event_count
        for node_id in failures:
            assert self.nodes[schedule_id][node_id]["state"] == "pending"
            self.nodes[schedule_id][node_id]["state"] = "failed"
        self._advance(schedule_id, checkpoint)

    def claim(self, schedule_id, node_id, owner, *, lease_expires_at, attempt_id=None):
        self.created_before_claim = schedule_id in self.schedules
        node = self.nodes[schedule_id][node_id]
        assert node["state"] == "ready"
        generation = node.get("generation", 0) + 1
        node.update(state="leased", generation=generation)
        identifier = attempt_id or str(uuid4())
        lease = SimpleNamespace(
            id=identifier,
            schedule_id=schedule_id,
            node_id=node_id,
            generation=generation,
            owner=owner,
            lease_expires_at=lease_expires_at,
            state="running",
        )
        self.attempts[identifier] = lease
        self.schedules[schedule_id]["event_count"] += 1
        return lease

    def heartbeat(self, attempt_id, generation, *, lease_expires_at):
        lease = self.attempts[attempt_id]
        assert lease.generation == generation
        lease.lease_expires_at = lease_expires_at
        self.schedules[lease.schedule_id]["event_count"] += 1
        return lease

    def settle_attempt(
        self,
        attempt_id,
        generation,
        outcome,
        *,
        reason,
        checkpoint,
        target_session_id=None,
        failure_kind=None,
    ):
        lease = self.attempts[attempt_id]
        assert lease.generation == generation
        lease.state = outcome
        self.nodes[lease.schedule_id][lease.node_id]["state"] = outcome
        self._advance(lease.schedule_id, checkpoint)

    def retry_failed(self, schedule_id, node_id, *, checkpoint, expected_event_count):
        assert self.schedules[schedule_id]["event_count"] == expected_event_count
        assert self.nodes[schedule_id][node_id]["state"] == "failed"
        self.nodes[schedule_id][node_id]["state"] = "ready"
        self._advance(schedule_id, checkpoint)

    def commit_reducer(self, attempt_id, generation, *, schema_id, result, checkpoint, **_):
        lease = self.attempts[attempt_id]
        assert lease.generation == generation
        lease.state = "completed"
        self.nodes[lease.schedule_id][lease.node_id]["state"] = "completed"
        self.results.setdefault(lease.schedule_id, {})[lease.node_id] = {
            "node_id": lease.node_id,
            "payload": result,
            "payload_digest": "sha256:test",
            "schema_id": schema_id,
        }
        self._advance(lease.schedule_id, checkpoint)
        return "sha256:test"

    def send_message(self, *args, **kwargs):
        return SimpleNamespace(id="message-1")

    def cancel_schedule(self, schedule_id, *, reason, cleanup_node_ids, checkpoint):
        self.schedules[schedule_id]["status"] = "cancelled"
        cleanup = set(cleanup_node_ids)
        for node_id, node in self.nodes[schedule_id].items():
            if node_id in cleanup and node["state"] in {"pending", "ready"}:
                node["state"] = "ready"
            elif node["state"] in {"pending", "ready", "leased"}:
                node["state"] = "cancelled"
        self._advance(schedule_id, checkpoint)

    def reject_schedule(self, schedule_id, *, reason):
        self.schedules[schedule_id]["status"] = "cancelled"
        for node in self.nodes[schedule_id].values():
            if node["state"] in {"pending", "ready", "leased"}:
                node["state"] = "cancelled"

    def cancel_node(self, schedule_id, node_id, *, reason, checkpoint, idempotency_key=None):
        receipt = (schedule_id, idempotency_key)
        previous = getattr(self, "cancel_receipts", {}).get(receipt)
        if previous is not None:
            if previous != (node_id, reason):
                raise ValueError("cancellation identity conflicts")
            return True
        node = self.nodes[schedule_id][node_id]
        if node["state"] in {"completed", "failed", "cancelled"}:
            return False
        node["state"] = "cancelled"
        for attempt in self.attempts.values():
            if attempt.schedule_id == schedule_id and attempt.node_id == node_id:
                attempt.state = "cancelled"
        self._advance(schedule_id, checkpoint)
        if idempotency_key is not None:
            if not hasattr(self, "cancel_receipts"):
                self.cancel_receipts = {}
            self.cancel_receipts[receipt] = (node_id, reason)
        return True

    def _advance(self, schedule_id, checkpoint):
        self.schedules[schedule_id]["checkpoint"] = checkpoint
        self.schedules[schedule_id]["event_count"] += 1


def _assignment(task_id: str, **overrides: object) -> WorkerAssignment:
    values = {
        "id": task_id,
        "objective": f"Assess {task_id}",
        "profile_id": "injection",
        "target_profile_id": "target",
        "technique_ids": (),
        "guidance_ids": (),
        "tool_ids": (),
        "skill_ids": (),
        "provider_id": "provider",
        "authority_ids": ("target:reviewed",),
        "budget": AssignmentBudget(4, 4_000, 60),
        "input_schema": '{"type":"object"}',
        "output_schema": '{"type":"object"}',
    }
    values.update(overrides)
    return WorkerAssignment(**values)  # type: ignore[arg-type]


def _same_session_assignment(task_id: str, agenda: str) -> WorkerAssignment:
    return _assignment(
        task_id,
        technique_ids=(f"technique-{agenda}",),
        agenda_id=f"agenda/{agenda}",
        execution_stage="control",
        pair_id=f"control-{agenda}",
        oracle_digest="sha256:" + "3" * 64,
        semantic_technique_id=f"technique-{agenda}",
        target_session_policy="same-reviewed-session",
        comparison_policy="reviewed-benign-differential-v1",
        reviewed_adapter_digest="sha256:" + "a" * 64,
        comparison_dimensions=(
            ("modality", "text"),
            ("turn", "single"),
            ("language", "und"),
        ),
    )


class RateLimitThenRecoverProvider(Provider):
    """Fail the first ``failures`` worker streams with rate limits, then succeed."""

    def __init__(self, failures: int) -> None:
        self.failures = failures
        self.active = 0
        self.maximum_active = 0
        self.recovered_peak = 0
        self._failed = 0

    async def stream(self, request):
        self.active += 1
        self.maximum_active = max(self.maximum_active, self.active)
        if self._failed >= self.failures:
            self.recovered_peak = max(self.recovered_peak, self.active)
        try:
            if self._failed < self.failures:
                self._failed += 1
                await asyncio.sleep(0.01)
                yield ModelError(message="slow down", code="rate_limited", retryable=False)
                return
            await asyncio.sleep(0.01)
            yield TextDelta(text='{"finding":"checked"}')
            yield ModelCompleted()
        finally:
            self.active -= 1


def test_rate_limit_halves_effective_concurrency_with_floor_one() -> None:
    controller = _AdaptiveConcurrency(6)

    controller.record_rate_limit()
    assert controller.effective == 3
    controller.record_rate_limit()
    assert controller.effective == 1
    controller.record_rate_limit()
    controller.record_rate_limit()
    assert controller.effective == 1


def test_recovery_ramps_gradually_and_never_exceeds_ceiling() -> None:
    controller = _AdaptiveConcurrency(4, ramp_successes=3)
    controller.record_rate_limit()
    controller.record_rate_limit()
    assert controller.effective == 1

    for _ in range(2):
        controller.record_success()
    assert controller.effective == 1
    controller.record_success()
    assert controller.effective == 2

    for _ in range(30):
        controller.record_success()
    assert controller.effective == 4


def test_non_rate_limit_failure_resets_ramp_without_lowering() -> None:
    controller = _AdaptiveConcurrency(4)
    controller.record_rate_limit()
    assert controller.effective == 2
    controller.record_success()
    controller.record_other_outcome()
    assert controller.effective == 2
    controller.record_success()
    controller.record_success()
    assert controller.effective == 2
    controller.record_success()
    assert controller.effective == 3


def test_rate_limit_failure_detection_uses_provider_error_code() -> None:
    rate_limited = WorkerResult(
        task_id="t", status="failed", error="slow down", error_code="rate_limited"
    )
    legacy_code = WorkerResult(task_id="t", status="failed", error="busy", error_code="rate_limit")
    other = WorkerResult(task_id="t", status="failed", error="boom", error_code="provider_error")
    completed = WorkerResult(task_id="t", status="completed")

    assert _is_rate_limit_failure(rate_limited)
    assert _is_rate_limit_failure(legacy_code)
    assert not _is_rate_limit_failure(other)
    assert not _is_rate_limit_failure(completed)


async def test_rate_limit_failures_narrow_waves_and_successes_ramp_back() -> None:
    store = Store()
    provider = RateLimitThenRecoverProvider(failures=4)
    service = DurableAgentService(provider, Tools(), store, model="test")
    assignments = tuple(_assignment(f"campaign/task-{index}") for index in range(12))

    live = await service.submit("session-1", assignments, max_workers=4)
    assert live.execution is not None
    await live.execution

    # The first wave ran at the reviewed width and hit every rate limit.
    assert provider.maximum_active == 4
    # Later waves narrowed below the ceiling, then ramped back as successes
    # accumulated, without ever re-exceeding the reviewed width.
    assert 1 < provider.recovered_peak < 4
    assert {node["state"] for node in store.nodes[live.id].values()} == {"completed"}


async def test_single_session_target_serializes_execution() -> None:
    store = Store()
    provider = RateLimitThenRecoverProvider(failures=0)
    service = DurableAgentService(provider, Tools(), store, model="test")
    assignments = tuple(
        _same_session_assignment(f"baseline/{agenda}/control", agenda)
        for agenda in ("one", "two", "three", "four")
    )

    live = await service.submit("session-1", assignments, max_workers=4)
    assert live.execution is not None
    await live.execution

    assert provider.maximum_active == 1
    outcomes = {attempt.state for attempt in store.attempts.values()}
    assert outcomes == {"completed"}


async def test_no_rate_limits_keep_the_reviewed_width() -> None:
    store = Store()
    provider = RateLimitThenRecoverProvider(failures=0)
    service = DurableAgentService(provider, Tools(), store, model="test")
    assignments = tuple(_assignment(f"campaign/task-{index}") for index in range(4))

    live = await service.submit("session-1", assignments, max_workers=4)
    assert live.execution is not None
    await live.execution

    assert provider.maximum_active == 4
    outcomes = {attempt.state for attempt in store.attempts.values()}
    assert outcomes == {"completed"}
