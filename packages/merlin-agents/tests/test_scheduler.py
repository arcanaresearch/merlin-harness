import json

import pytest
from merlin_agents import (
    LeaseError,
    NodeStatus,
    NotReadyError,
    ReferenceScheduler,
    RetryClass,
    RetryFailureKind,
    RetryPolicy,
    SchedulerConfig,
    SchedulerEdge,
    SchedulerEdgeKind,
    SchedulerError,
    SchedulerNode,
    StaleGenerationError,
)


def test_dependency_readiness_and_cleanup_after_failure() -> None:
    scheduler = ReferenceScheduler(
        [SchedulerNode("start"), SchedulerNode("work"), SchedulerNode("cleanup")],
        [
            SchedulerEdge("start", "work"),
            SchedulerEdge("work", "cleanup", SchedulerEdgeKind.CLEANUP),
        ],
    )
    assert scheduler.ready_nodes() == ("start",)
    start = scheduler.claim("start", "worker-a", 0)
    scheduler.complete(start, "start-result", 1)
    work = scheduler.claim("work", "worker-b", 1)
    scheduler.fail(work, "denied", 2)
    assert scheduler.status("work") == NodeStatus.FAILED
    assert scheduler.ready_nodes() == ("cleanup",)


def test_failed_prerequisite_terminally_blocks_dependent_chain() -> None:
    scheduler = ReferenceScheduler(
        [SchedulerNode("recon"), SchedulerNode("specialist"), SchedulerNode("followup")],
        [
            SchedulerEdge("recon", "specialist"),
            SchedulerEdge("specialist", "followup"),
        ],
    )
    recon = scheduler.claim("recon", "worker-a", 0)

    scheduler.fail(recon, "campaign budget exhausted", 1)

    assert scheduler.status("specialist") is NodeStatus.FAILED
    assert scheduler.states["specialist"].disposition is not None
    assert scheduler.states["specialist"].disposition.reason == "prerequisite 'recon' failed"
    assert scheduler.status("followup") is NodeStatus.FAILED
    assert scheduler.states["followup"].disposition is not None
    assert scheduler.states["followup"].disposition.reason == "prerequisite 'specialist' failed"
    assert scheduler.ready_nodes() == ()


def test_order_only_edge_continues_after_failed_serialized_worker() -> None:
    scheduler = ReferenceScheduler(
        [SchedulerNode("first"), SchedulerNode("next")],
        [SchedulerEdge("first", "next", SchedulerEdgeKind.ORDER_ONLY)],
    )

    first = scheduler.claim("first", "worker-a", 0)
    scheduler.fail(first, "isolated technique failure", 1)

    assert scheduler.status("first") is NodeStatus.FAILED
    assert scheduler.status("next") is NodeStatus.READY


def test_restore_terminally_blocks_pending_dependent_of_failed_prerequisite() -> None:
    nodes = [SchedulerNode("recon"), SchedulerNode("specialist")]
    edges = [SchedulerEdge("recon", "specialist")]
    scheduler = ReferenceScheduler(nodes, edges)
    recon = scheduler.claim("recon", "worker-a", 0)
    scheduler.fail(recon, "campaign budget exhausted", 1)
    checkpoint = scheduler.checkpoint()
    checkpoint["nodes"]["specialist"]["status"] = "pending"
    checkpoint["nodes"]["specialist"]["disposition"] = None

    restored = ReferenceScheduler.restore(nodes, edges, checkpoint)

    assert restored.status("specialist") is NodeStatus.FAILED
    assert restored.states["specialist"].disposition is not None
    assert restored.states["specialist"].disposition.reason == "prerequisite 'recon' failed"


def test_bounded_failure_retry_uses_a_new_generation() -> None:
    scheduler = ReferenceScheduler([SchedulerNode("work", max_attempts=2)], [])
    first = scheduler.claim("work", "one", 0)
    scheduler.fail(first, "transient", 1)
    second = scheduler.claim("work", "two", 1)
    assert second.generation == first.generation + 1
    scheduler.fail(second, "permanent", 2)
    assert scheduler.status("work") == NodeStatus.FAILED
    with pytest.raises(NotReadyError):
        scheduler.claim("work", "three", 3)


def test_heartbeat_expiry_reassignment_and_stale_generation_quarantine() -> None:
    scheduler = ReferenceScheduler(
        [SchedulerNode("work")],
        [],
        SchedulerConfig(lease_ttl_ticks=5, max_lease_reassignments_per_node=1),
    )
    first = scheduler.claim("work", "one", 0)
    scheduler.heartbeat(first, 3)
    with pytest.raises(LeaseError, match="still live"):
        scheduler.reassign_expired("work", "two", 7)
    second = scheduler.reassign_expired("work", "two", 8)
    assert second is not None and second.generation == 2
    with pytest.raises(StaleGenerationError):
        scheduler.complete(first, "late", 8)
    assert scheduler.quarantine[-1].reason == "stale-generation"
    scheduler.complete(second, "current", 9)


def test_external_lease_confirmation_resynchronizes_only_current_generation() -> None:
    scheduler = ReferenceScheduler(
        [SchedulerNode("work")],
        [],
        SchedulerConfig(lease_ttl_ticks=2),
    )
    first = scheduler.claim("work", "one", 0)

    # An external wall-clock authority confirmed this exact owner after the
    # local logical tick window elapsed.
    scheduler.confirm_external_lease(first, 3)
    scheduler.complete(first, "current", 4)

    assert scheduler.status("work") is NodeStatus.COMPLETED

    with pytest.raises(StaleGenerationError):
        scheduler.confirm_external_lease(
            type(first)(first.node_id, first.owner_id, first.generation + 1, "replacement"),
            5,
        )


def test_reassignment_cap_terminally_fails_node() -> None:
    scheduler = ReferenceScheduler(
        [SchedulerNode("work")],
        [],
        SchedulerConfig(lease_ttl_ticks=1, max_lease_reassignments_per_node=0),
    )
    scheduler.claim("work", "one", 0)
    assert scheduler.reassign_expired("work", "two", 1) is None
    assert scheduler.status("work") == NodeStatus.FAILED


def test_expiry_reopens_node_and_fences_late_result_before_replacement() -> None:
    scheduler = ReferenceScheduler(
        [SchedulerNode("work", max_attempts=2)],
        [],
        SchedulerConfig(lease_ttl_ticks=2),
    )
    first = scheduler.claim("work", "one", 0)
    scheduler.expire("work", 2)
    assert scheduler.status("work") == NodeStatus.READY
    second = scheduler.claim("work", "two", 2)
    assert second.generation == first.generation + 1
    with pytest.raises(StaleGenerationError):
        scheduler.complete(first, "late", 2)
    scheduler.complete(second, "replacement", 3)


def test_node_cancellation_runs_cleanup_and_schedule_cancel_revokes_all() -> None:
    scheduler = ReferenceScheduler(
        [SchedulerNode("work"), SchedulerNode("cleanup")],
        [SchedulerEdge("work", "cleanup", SchedulerEdgeKind.CLEANUP)],
    )
    worker = scheduler.claim("work", "one", 0)
    scheduler.cancel("work", "operator", 1)
    assert scheduler.ready_nodes() == ("cleanup",)
    assert scheduler.quarantine[-1].handle == worker
    scheduler.cancel_schedule("shutdown", 2)
    assert scheduler.status("cleanup") == NodeStatus.CANCELED


def test_retry_classes_preserve_provider_turn_replace_worker_and_forbid_effect_replay() -> None:
    scheduler = ReferenceScheduler([SchedulerNode("work", max_attempts=2)], [])
    first = scheduler.claim("work", "one", 0)
    assert scheduler.admit_retry(first, RetryClass.SAFE_PROVIDER, "busy", 1) == first
    assert scheduler.status("work") == NodeStatus.LEASED

    assert scheduler.admit_retry(first, RetryClass.FRESH_WORKER, "worker crashed", 2) is None
    replacement = scheduler.claim("work", "two", 2)
    assert replacement.generation == first.generation + 1
    assert scheduler.quarantine[-1].reason == "worker-replaced"

    scheduler.admit_retry(
        replacement, RetryClass.EFFECT_REPLAY_FORBIDDEN, "target may have changed", 3
    )
    assert scheduler.status("work") == NodeStatus.FAILED
    assert scheduler.quarantine[-1].reason == "effect-replay-forbidden"


def test_terminal_retry_class_does_not_consume_a_replacement_attempt() -> None:
    scheduler = ReferenceScheduler([SchedulerNode("worker", max_attempts=2)], [])
    first = scheduler.claim("worker", "agent", 0)

    assert scheduler.admit_retry(first, RetryClass.TERMINAL, "budget exhausted", 1) is None
    assert scheduler.status("worker") is NodeStatus.FAILED
    assert scheduler.states["worker"].attempts_started == 1
    assert scheduler.quarantine[-1].reason == "terminal-failure"


def test_typed_retry_requeues_progress_but_stops_repeated_stalls() -> None:
    scheduler = ReferenceScheduler(
        [
            SchedulerNode(
                "work",
                max_attempts=None,
                retry_policy=RetryPolicy(
                    frozenset({RetryFailureKind.ADAPTER_TIMEOUT}),
                    max_consecutive_stalls=2,
                ),
            )
        ],
        [],
    )
    first = scheduler.claim("work", "one", 0)
    scheduler.admit_retry(
        first,
        RetryClass.FRESH_WORKER,
        "timeout",
        1,
        failure_kind=RetryFailureKind.ADAPTER_TIMEOUT,
        progress_marker="checkpoint-a",
    )
    second = scheduler.claim("work", "two", 1)
    scheduler.admit_retry(
        second,
        RetryClass.FRESH_WORKER,
        "timeout",
        2,
        failure_kind=RetryFailureKind.ADAPTER_TIMEOUT,
        progress_marker="checkpoint-a",
    )
    third = scheduler.claim("work", "three", 2)
    scheduler.admit_retry(
        third,
        RetryClass.FRESH_WORKER,
        "timeout",
        3,
        failure_kind=RetryFailureKind.ADAPTER_TIMEOUT,
        progress_marker="checkpoint-a",
    )
    fourth = scheduler.claim("work", "four", 3)
    scheduler.admit_retry(
        fourth,
        RetryClass.FRESH_WORKER,
        "timeout",
        4,
        failure_kind=RetryFailureKind.ADAPTER_TIMEOUT,
        progress_marker="checkpoint-a",
    )

    assert scheduler.status("work") is NodeStatus.FAILED
    assert scheduler.states["work"].attempts_started == 4
    assert scheduler.states["work"].disposition is not None
    assert "retry stalled" in (scheduler.states["work"].disposition.reason or "")


def test_missing_target_effect_fails_fast_without_replacement() -> None:
    scheduler = ReferenceScheduler(
        [
            SchedulerNode(
                "work",
                max_attempts=None,
                retry_policy=RetryPolicy(
                    frozenset({RetryFailureKind.MISSING_TARGET_EFFECT}),
                    failure_kind_stall_limits={RetryFailureKind.MISSING_TARGET_EFFECT: 0},
                ),
            )
        ],
        [],
    )
    first = scheduler.claim("work", "one", 0)
    replacement = scheduler.admit_retry(
        first,
        RetryClass.FRESH_WORKER,
        "worker completed without executing a required target effect",
        1,
        failure_kind=RetryFailureKind.MISSING_TARGET_EFFECT,
    )

    assert replacement is None
    assert scheduler.status("work") is NodeStatus.FAILED
    assert scheduler.states["work"].attempts_started == 1
    assert "retry stalled after 0 replacements" in (
        scheduler.states["work"].disposition.reason or ""
    )


def test_schema_invalid_allows_one_replacement_then_fails_fast() -> None:
    scheduler = ReferenceScheduler(
        [
            SchedulerNode(
                "work",
                max_attempts=None,
                retry_policy=RetryPolicy(
                    frozenset({RetryFailureKind.RESULT_SCHEMA_INVALID}),
                    failure_kind_stall_limits={RetryFailureKind.RESULT_SCHEMA_INVALID: 1},
                ),
            )
        ],
        [],
    )
    first = scheduler.claim("work", "one", 0)
    replacement = scheduler.admit_retry(
        first,
        RetryClass.FRESH_WORKER,
        "result schema validation failed: not parseable JSON",
        1,
        failure_kind=RetryFailureKind.RESULT_SCHEMA_INVALID,
    )

    assert replacement is None
    assert scheduler.status("work") is NodeStatus.READY

    second = scheduler.claim("work", "two", 1)
    exhausted = scheduler.admit_retry(
        second,
        RetryClass.FRESH_WORKER,
        "result schema validation failed: not parseable JSON",
        2,
        failure_kind=RetryFailureKind.RESULT_SCHEMA_INVALID,
    )

    assert exhausted is None
    assert scheduler.status("work") is NodeStatus.FAILED
    assert scheduler.states["work"].attempts_started == 2


@pytest.mark.parametrize(
    "failure_kind",
    [
        RetryFailureKind.AUTHORIZATION,
        RetryFailureKind.SCOPE,
        RetryFailureKind.STATE_INTEGRITY,
        RetryFailureKind.BUDGET,
    ],
)
def test_typed_retry_does_not_requeue_terminal_failure_kinds(
    failure_kind: RetryFailureKind,
) -> None:
    scheduler = ReferenceScheduler(
        [
            SchedulerNode(
                "work",
                max_attempts=None,
                retry_policy=RetryPolicy(frozenset({RetryFailureKind.ADAPTER_TIMEOUT})),
            )
        ],
        [],
    )
    attempt = scheduler.claim("work", "one", 0)

    scheduler.admit_retry(
        attempt,
        RetryClass.FRESH_WORKER,
        failure_kind.value,
        1,
        failure_kind=failure_kind,
        budget_exhausted=failure_kind is RetryFailureKind.BUDGET,
    )

    assert scheduler.status("work") is NodeStatus.FAILED


def test_typed_retry_stall_state_survives_checkpoint_restore() -> None:
    nodes = [
        SchedulerNode(
            "work",
            max_attempts=None,
            retry_policy=RetryPolicy(
                frozenset({RetryFailureKind.TRANSPORT_TRANSIENT}),
                max_consecutive_stalls=1,
            ),
        )
    ]
    scheduler = ReferenceScheduler(nodes, [])
    first = scheduler.claim("work", "one", 0)
    scheduler.admit_retry(
        first,
        RetryClass.FRESH_WORKER,
        "transport reset",
        1,
        failure_kind=RetryFailureKind.TRANSPORT_TRANSIENT,
    )

    restored = ReferenceScheduler.restore(nodes, [], scheduler.checkpoint())
    second = restored.claim("work", "two", 1)
    restored.admit_retry(
        second,
        RetryClass.FRESH_WORKER,
        "transport reset",
        2,
        failure_kind=RetryFailureKind.TRANSPORT_TRANSIENT,
    )

    assert restored.status("work") is NodeStatus.FAILED
    assert restored.states["work"].consecutive_retry_stalls == 2


@pytest.mark.parametrize(
    "stored_kind",
    (RetryFailureKind.CLEANUP_UNRESOLVED.value, None),
)
def test_retry_failure_kind_valid_and_null_values_survive_checkpoint_restore(
    stored_kind: str | None,
) -> None:
    nodes = [SchedulerNode("cleanup")]
    scheduler = ReferenceScheduler(nodes, [])
    checkpoint = scheduler.checkpoint()
    checkpoint["nodes"]["cleanup"]["last_retry_failure_kind"] = stored_kind

    restored = ReferenceScheduler.restore(nodes, [], checkpoint)

    assert restored.states["cleanup"].last_retry_failure_kind == stored_kind


def test_missing_retry_failure_kind_remains_backward_compatible() -> None:
    nodes = [SchedulerNode("cleanup")]
    scheduler = ReferenceScheduler(nodes, [])
    checkpoint = scheduler.checkpoint()
    del checkpoint["nodes"]["cleanup"]["last_retry_failure_kind"]

    restored = ReferenceScheduler.restore(nodes, [], checkpoint)

    assert restored.states["cleanup"].last_retry_failure_kind is None


def test_checkpoint_restore_rejects_unknown_retry_failure_kind() -> None:
    nodes = [SchedulerNode("cleanup")]
    scheduler = ReferenceScheduler(nodes, [])
    checkpoint = scheduler.checkpoint()
    checkpoint["nodes"]["cleanup"]["last_retry_failure_kind"] = "invented"

    with pytest.raises(SchedulerError, match="retry failure kind is invalid"):
        ReferenceScheduler.restore(nodes, [], checkpoint)


def test_deadline_fences_live_attempt_and_unblocks_cleanup() -> None:
    scheduler = ReferenceScheduler(
        [SchedulerNode("work", deadline_tick=3), SchedulerNode("cleanup")],
        [SchedulerEdge("work", "cleanup", SchedulerEdgeKind.CLEANUP)],
    )
    attempt = scheduler.claim("work", "one", 0)
    assert scheduler.expire_deadlines(3) == ("work",)
    assert scheduler.status("work") == NodeStatus.FAILED
    assert scheduler.ready_nodes() == ("cleanup",)
    assert scheduler.quarantine[-1].handle == attempt
    with pytest.raises(StaleGenerationError):
        scheduler.complete(attempt, "late", 3)


def test_heartbeat_cannot_extend_work_past_node_deadline() -> None:
    scheduler = ReferenceScheduler([SchedulerNode("work", deadline_tick=2)], [])
    attempt = scheduler.claim("work", "one", 0)
    with pytest.raises(LeaseError, match="deadline exceeded"):
        scheduler.heartbeat(attempt, 2)
    assert scheduler.status("work") == NodeStatus.FAILED


def test_claim_next_respects_global_and_resource_limits() -> None:
    scheduler = ReferenceScheduler(
        [
            SchedulerNode("a", resource_key="provider"),
            SchedulerNode("b", resource_key="provider"),
            SchedulerNode("c", resource_key="other"),
        ],
        [],
        SchedulerConfig(max_concurrent_leases=2, max_concurrent_per_resource=1),
    )
    assert scheduler.claim_next("one", 0).node_id == "a"
    assert scheduler.claim_next("two", 0).node_id == "c"
    assert scheduler.claim_next("three", 0) is None


def test_checkpoint_restores_fair_ready_order() -> None:
    nodes = [SchedulerNode("a", max_attempts=2), SchedulerNode("b")]
    scheduler = ReferenceScheduler(nodes, [])
    a = scheduler.claim("a", "one", 0)
    scheduler.fail(a, "retry", 4)
    restored = ReferenceScheduler.restore(nodes, [], scheduler.checkpoint())
    assert restored.ready_nodes() == ("b", "a")


def test_fan_in_is_canonical_and_preserves_all_branch_dispositions() -> None:
    scheduler = ReferenceScheduler(
        [SchedulerNode("b"), SchedulerNode("a"), SchedulerNode("reduce")],
        [
            SchedulerEdge("b", "reduce", SchedulerEdgeKind.FAN_IN),
            SchedulerEdge("a", "reduce", SchedulerEdgeKind.FAN_IN),
        ],
    )
    b = scheduler.claim("b", "worker-b", 0)
    a = scheduler.claim("a", "worker-a", 0)
    scheduler.complete(b, "evidence-b", 1)
    scheduler.fail(a, "blocked", 1)
    fan_in = scheduler.fan_in("reduce")
    branches = json.loads(fan_in.manifest)["branches"]
    assert [branch["node_id"] for branch in branches] == ["a", "b"]
    assert branches[0]["status"] == "failed"
    assert branches[1]["result"] == "evidence-b"
    assert fan_in.digest.startswith("sha256:")
