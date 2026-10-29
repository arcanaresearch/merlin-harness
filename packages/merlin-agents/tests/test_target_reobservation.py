import hashlib
import json
from dataclasses import asdict

from merlin_agents.target_reobservation import (
    extract_target_reobservation_obligations,
    project_target_reobservations,
    target_reobservation_schedule_id,
)

CAMPAIGN_ID = "campaign-1"
SESSION_ID = "session-1"
TARGET_ID = "target-1"
TARGET_SESSION_ID = "browser:context-1"
TASK_ID = "task-1"


def _digest(value: object) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return "sha256:" + hashlib.sha256(encoded.encode()).hexdigest()


def _lead_decision_row(
    bindings: list[dict[str, str]],
    *,
    wave: int = 1,
    source_message_ids: tuple[str, ...] = (),
) -> dict[str, object]:
    payload: dict[str, object] = {
        "schema": "merlin.campaign-lead-decision.v1",
        "campaign_id": CAMPAIGN_ID,
        "wave": wave,
        "source_message_ids": list(sorted(set(source_message_ids))),
        "target_reobservation_generations": bindings,
    }
    identity = json.dumps(
        {
            "campaign_id": CAMPAIGN_ID,
            "wave": wave,
            "source_message_ids": tuple(sorted(set(source_message_ids))),
            "payload": payload,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return {
        "wave": wave,
        "decision_id": "lead-decision:" + hashlib.sha256(identity.encode()).hexdigest(),
        "payload": payload,
    }


def _transition(*, adapter_stale: bool, include_extra: bool = False) -> dict[str, object]:
    before_digest = "sha256:" + "1" * 64
    after_digest = "sha256:" + "2" * 64
    transition: dict[str, object] = {
        "schema": "merlin.target-ui-transition.v1",
        "material": True,
        "adapter_stale": adapter_stale,
        "before_digest": before_digest,
        "after_digest": after_digest,
        "changed_dimensions": ["controls"],
        "added_controls": [
            {
                "role": "button",
                "name": "Keep Playing",
                "frame_ref": "main",
            },
            {
                "role": "button",
                "name": "Next Level",
                "frame_ref": "main",
            },
        ],
        "removed_controls": [],
        "controls_truncated": False,
        "evidence_refs": ["browser-snapshot:post-effect"],
        "reobservation_recommended": True,
    }
    if include_extra:
        transition["unbounded_extra"] = {
            "instruction": "click Next Level",
            "nested": ["must not reach the journal"],
        }
        transition["added_controls"][0]["unbounded_control_extra"] = "drop me"  # type: ignore[index]
    return transition


def _receipt(
    *,
    call_id: str,
    task_id: str = TASK_ID,
    adapter_stale: bool,
    zero_effect: bool = False,
    attempted_effect: bool = False,
    include_extra: bool = False,
    target_session_id: str = TARGET_SESSION_ID,
) -> dict[str, object]:
    body: dict[str, object] = {
        "call_id": call_id,
        "reference": f"target-call:{call_id}",
        "assignment_id": task_id,
        "campaign_id": CAMPAIGN_ID,
        "session_id": SESSION_ID,
        "target_id": TARGET_ID,
        "target_session_id": target_session_id,
        "tool_name": "browser_chat_exchange",
        "is_error": zero_effect or attempted_effect,
        "request_sha256": "3" * 64,
        "content_sha256": "4" * 64,
        "metadata_sha256": "5" * 64,
        "target_ui_transition": _transition(
            adapter_stale=adapter_stale,
            include_extra=include_extra,
        ),
    }
    if zero_effect:
        body.update(
            {
                "target_effect_attempted": False,
                "interaction_count": 0,
                "limit_interaction_count": 0,
                "target_call_budget_charge": 0,
            }
        )
    elif attempted_effect:
        body.update(
            {
                "target_effect_attempted": True,
                "interaction_count": 1,
                "limit_interaction_count": 1,
                "target_call_budget_charge": 1,
            }
        )
    return {**body, "receipt_sha256": _digest(body).removeprefix("sha256:")}


def _resign(receipt: dict[str, object]) -> dict[str, object]:
    body = {key: value for key, value in receipt.items() if key != "receipt_sha256"}
    return {**body, "receipt_sha256": _digest(body).removeprefix("sha256:")}


def _extract(
    receipt: dict[str, object],
    *,
    source_schedule_id: str = "schedule-1",
    projection: str = "target_evidence_receipts",
) -> tuple:
    call_id = str(receipt["call_id"])
    return extract_target_reobservation_obligations(
        campaign_id=CAMPAIGN_ID,
        session_id=SESSION_ID,
        target_id=TARGET_ID,
        source_schedule_id=source_schedule_id,
        source_wave=1,
        task_ids=[TASK_ID],
        evidence={TASK_ID: {projection: {call_id: receipt}}},
    )


def test_accepts_lakera_like_success_transition_and_projects_only_bounded_fields() -> None:
    obligations = _extract(
        _receipt(
            call_id="call-success",
            adapter_stale=False,
            include_extra=True,
        )
    )

    assert len(obligations) == 1
    obligation = obligations[0]
    assert obligation.receipt_projection == "target_evidence_receipts"
    assert obligation.transition["adapter_stale"] is False
    assert obligation.transition["added_controls"] == [
        {
            "role": "button",
            "name": "Keep Playing",
            "frame_ref": "main",
        },
        {
            "role": "button",
            "name": "Next Level",
            "frame_ref": "main",
        },
    ]
    assert "unbounded_extra" not in obligation.transition


def test_strict_zero_effect_transition_requires_stale_adapter() -> None:
    accepted = _extract(
        _receipt(call_id="call-stale", adapter_stale=True, zero_effect=True),
        projection="target_ui_transition_receipts",
    )
    rejected = _extract(
        _receipt(call_id="call-fresh", adapter_stale=False, zero_effect=True),
        projection="target_ui_transition_receipts",
    )

    assert len(accepted) == 1
    assert accepted[0].receipt_projection == "target_ui_transition_receipts"
    assert accepted[0].transition["adapter_stale"] is True
    assert rejected == ()


def test_attempted_effect_error_accepts_fresh_adapter_without_attack_credit() -> None:
    obligations = _extract(
        _receipt(
            call_id="call-attempted-effect",
            adapter_stale=False,
            attempted_effect=True,
        ),
        projection="target_ui_transition_receipts",
    )

    assert len(obligations) == 1
    obligation = obligations[0]
    assert asdict(obligation).keys() == {
        "campaign_id",
        "session_id",
        "target_id",
        "target_session_id",
        "event_digest",
        "transition_digest",
        "source_schedule_id",
        "source_wave",
        "source_task_id",
        "target_call_id",
        "receipt_sha256",
        "receipt_projection",
        "transition",
    }
    assert obligation.receipt_projection == "target_ui_transition_receipts"
    assert obligation.transition["adapter_stale"] is False

    observed = obligation.observed_payload(CAMPAIGN_ID, TARGET_ID)
    assert observed["kind"] == "target_surface_transition_observed"
    assert {
        "success",
        "attack_credit",
        "attack_outcome",
        "target_effect_attempted",
        "score",
        "finding",
    }.isdisjoint(observed)


def test_generic_post_effect_capture_error_creates_reobservation_obligation() -> None:
    receipt = _receipt(
        call_id="call-generic-capture-failed",
        adapter_stale=True,
        attempted_effect=True,
    )
    receipt.update(
        {
            "tool_name": "browser_click",
            "interaction_count": 0,
            "limit_interaction_count": 0,
        }
    )
    receipt = _resign(receipt)

    obligations = _extract(receipt, projection="target_ui_transition_receipts")

    assert len(obligations) == 1
    obligation = obligations[0]
    assert obligation.target_call_id == "call-generic-capture-failed"
    assert obligation.transition["adapter_stale"] is True
    assert obligation.receipt_projection == "target_ui_transition_receipts"


def test_attempted_effect_projection_rejects_malformed_unsigned_and_non_error_receipts() -> None:
    malformed = _receipt(
        call_id="call-malformed",
        adapter_stale=False,
        attempted_effect=True,
    )
    transition = malformed["target_ui_transition"]
    assert isinstance(transition, dict)
    transition["material"] = False
    malformed = _resign(malformed)

    unsigned = _receipt(
        call_id="call-unsigned",
        adapter_stale=False,
        attempted_effect=True,
    )
    unsigned.pop("receipt_sha256")

    non_error = _receipt(
        call_id="call-non-error",
        adapter_stale=False,
        attempted_effect=True,
    )
    non_error["is_error"] = False
    non_error = _resign(non_error)

    for receipt in (malformed, unsigned, non_error):
        assert _extract(receipt, projection="target_ui_transition_receipts") == ()


def test_receipt_from_another_session_is_rejected_even_when_resigned() -> None:
    wrong_session = _receipt(call_id="call-wrong-session", adapter_stale=False)
    wrong_session["session_id"] = "session-foreign"
    wrong_session = _resign(wrong_session)

    assert _extract(wrong_session) == ()


def test_exact_receipt_replay_dedupes_across_replacement_schedules() -> None:
    receipt = _receipt(call_id="call-replayed", adapter_stale=False)
    first = _extract(receipt, source_schedule_id="schedule-original")[0]
    replacement = _extract(receipt, source_schedule_id="schedule-replacement")[0]

    assert first.event_digest == replacement.event_digest
    projection = project_target_reobservations(
        [
            {"payload": first.observed_payload(CAMPAIGN_ID, TARGET_ID)},
            {"payload": replacement.observed_payload(CAMPAIGN_ID, TARGET_ID)},
        ]
    )
    assert len(projection.pending) == 1
    assert projection.pending[0].event_digest == first.event_digest


def test_projection_rejects_untyped_observation_and_start_rows() -> None:
    obligation = _extract(_receipt(call_id="call-schema", adapter_stale=False))[0]
    observed_payload = obligation.observed_payload(CAMPAIGN_ID, TARGET_ID)
    assert observed_payload["schema"] == "merlin.target-surface-transition-observed.v2"
    legacy_observation = dict(observed_payload)
    legacy_observation["schema"] = "merlin.target-surface-transition-observed.v1"
    assert project_target_reobservations([{"payload": legacy_observation}]).pending == ()
    legacy_observation.pop("target_session_id")
    assert project_target_reobservations([{"payload": legacy_observation}]).pending == ()
    untyped_observation = dict(observed_payload)
    untyped_observation.pop("schema")
    assert project_target_reobservations([{"payload": untyped_observation}]).pending == ()

    rows: list[dict[str, object]] = [{"sequence": 1, "payload": observed_payload}]
    generation = project_target_reobservations(rows).current_generation
    assert generation is not None
    untyped_start = {
        "kind": "target_reobservation_started",
        "wave_kind": "objective_prerequisite",
        "campaign_id": CAMPAIGN_ID,
        "session_id": SESSION_ID,
        "target_id": TARGET_ID,
        "target_session_id": TARGET_SESSION_ID,
        "generation_id": generation.generation_id,
        "obligation_id": generation.obligation_id,
        "event_digests": generation.event_digests,
        "event_set_digest": _digest({"event_digests": generation.event_digests}),
        "observation_cutoff_sequence": generation.observation_cutoff_sequence,
        "source_wave": generation.source_wave,
        "attempt": 1,
        "schedule_id": target_reobservation_schedule_id(CAMPAIGN_ID, generation.generation_id, 1),
    }
    projection = project_target_reobservations([*rows, {"sequence": 2, "payload": untyped_start}])
    assert projection.generations == {}
    assert projection.starts == {}


def test_identical_transition_shape_creates_one_obligation_per_authenticated_event() -> None:
    first_receipt = _receipt(call_id="call-level-1", adapter_stale=False)
    second_receipt = _receipt(call_id="call-level-2", adapter_stale=False)
    obligations = extract_target_reobservation_obligations(
        campaign_id=CAMPAIGN_ID,
        session_id=SESSION_ID,
        target_id=TARGET_ID,
        source_schedule_id="schedule-1",
        source_wave=1,
        task_ids=[TASK_ID],
        evidence={
            TASK_ID: {
                "target_evidence_receipts": {
                    "call-level-1": first_receipt,
                    "call-level-2": second_receipt,
                }
            }
        },
    )

    assert len(obligations) == 2
    assert len({item.transition_digest for item in obligations}) == 1
    assert len({item.event_digest for item in obligations}) == 2

    projection = project_target_reobservations(
        [
            {
                "sequence": index,
                "payload": obligation.observed_payload(CAMPAIGN_ID, TARGET_ID),
            }
            for index, obligation in enumerate(obligations, 1)
        ]
    )
    generation = projection.current_generation
    assert generation is not None
    assert len(generation.event_digests) == 2
    assert generation.event_digests == tuple(item.event_digest for item in obligations)


def test_generations_partition_target_sessions_and_advance_after_review() -> None:
    first = _extract(
        _receipt(
            call_id="call-context-first",
            adapter_stale=False,
            target_session_id=TARGET_SESSION_ID,
        )
    )[0]
    second_target_session = "browser:context-2"
    second = _extract(
        _receipt(
            call_id="call-context-second",
            adapter_stale=False,
            target_session_id=second_target_session,
        )
    )[0]
    rows: list[dict[str, object]] = [
        {"sequence": 1, "payload": first.observed_payload(CAMPAIGN_ID, TARGET_ID)},
        {"sequence": 2, "payload": second.observed_payload(CAMPAIGN_ID, TARGET_ID)},
    ]
    projected = project_target_reobservations(rows)
    generation = projected.current_generation
    assert generation is not None
    assert generation.target_session_id == TARGET_SESSION_ID
    assert generation.event_digests == (first.event_digest,)
    assert tuple(item.event_digest for item in projected.pending) == (
        first.event_digest,
        second.event_digest,
    )

    event_set_digest = _digest({"event_digests": generation.event_digests})
    schedule_id = target_reobservation_schedule_id(CAMPAIGN_ID, generation.generation_id, 1)
    evidence_refs = ["browser-snapshot:first-context-current"]
    settlement_id = "target-reobservation-settled/" + generation.generation_id.removeprefix(
        "sha256:"
    )
    rows.extend(
        (
            {
                "sequence": 3,
                "payload": {
                    "kind": "target_reobservation_started",
                    "schema": "merlin.target-reobservation-started.v1",
                    "wave_kind": "objective_prerequisite",
                    "campaign_id": CAMPAIGN_ID,
                    "session_id": SESSION_ID,
                    "target_id": TARGET_ID,
                    "target_session_id": TARGET_SESSION_ID,
                    "generation_id": generation.generation_id,
                    "obligation_id": generation.obligation_id,
                    "event_digests": list(generation.event_digests),
                    "event_set_digest": event_set_digest,
                    "observation_cutoff_sequence": generation.observation_cutoff_sequence,
                    "source_wave": generation.source_wave,
                    "attempt": 1,
                    "schedule_id": schedule_id,
                },
            },
            {
                "sequence": 4,
                "payload": {
                    "kind": "target_reobservation_settled",
                    "schema": "merlin.target-reobservation-settlement.v1",
                    "campaign_id": CAMPAIGN_ID,
                    "session_id": SESSION_ID,
                    "target_id": TARGET_ID,
                    "target_session_id": TARGET_SESSION_ID,
                    "generation_id": generation.generation_id,
                    "obligation_id": generation.obligation_id,
                    "event_digests": list(generation.event_digests),
                    "event_set_digest": event_set_digest,
                    "event_count": len(generation.event_digests),
                    "observation_cutoff_sequence": generation.observation_cutoff_sequence,
                    "source_wave": generation.source_wave,
                    "status": "refreshed",
                    "attempt": 1,
                    "schedule_id": schedule_id,
                    "settlement_id": settlement_id,
                    "current_surface_digest": "sha256:" + "6" * 64,
                    "surface_evidence_digest": _digest({"evidence_refs": evidence_refs}),
                    "surface_evidence_refs": evidence_refs,
                    "recon_handoff_id": "handoff-first-context",
                    "compiled_adapter_digest": "sha256:" + "7" * 64,
                    "reviewed_adapter_digest": "sha256:" + "8" * 64,
                    "result_reference": "task-result:first-context",
                    "result_digest": "sha256:" + "9" * 64,
                },
            },
            {
                "sequence": 5,
                **_lead_decision_row(
                    [
                        {
                            "generation_id": generation.generation_id,
                            "settlement_id": settlement_id,
                            "target_session_id": TARGET_SESSION_ID,
                        }
                    ]
                ),
            },
        )
    )
    projected = project_target_reobservations(rows)
    assert projected.reviewed == {
        generation.generation_id: rows[-1]["decision_id"],
    }
    assert tuple(item.event_digest for item in projected.pending) == (second.event_digest,)
    next_generation = projected.current_generation
    assert next_generation is not None
    assert next_generation.target_session_id == second_target_session
    assert next_generation.event_digests == (second.event_digest,)


def test_projection_blocks_retry_until_authorized_then_closes_after_refresh() -> None:
    obligations = extract_target_reobservation_obligations(
        campaign_id=CAMPAIGN_ID,
        session_id=SESSION_ID,
        target_id=TARGET_ID,
        source_schedule_id="schedule-1",
        source_wave=1,
        task_ids=[TASK_ID],
        evidence={
            TASK_ID: {
                "target_evidence_receipts": {
                    call_id: _receipt(call_id=call_id, adapter_stale=False)
                    for call_id in ("call-lifecycle-1", "call-lifecycle-2")
                }
            }
        },
    )
    rows: list[dict[str, object]] = [
        {
            "sequence": index,
            "payload": obligation.observed_payload(CAMPAIGN_ID, TARGET_ID),
        }
        for index, obligation in enumerate(obligations, 1)
    ]

    observed = project_target_reobservations(rows)
    assert observed.pending == obligations
    generation = observed.current_generation
    assert generation is not None
    assert len(generation.event_digests) == 2
    assert observed.eligible(generation.generation_id) is True
    event_set_digest = _digest({"event_digests": generation.event_digests})

    def started_payload(attempt: int, *, schedule_id: str | None = None) -> dict[str, object]:
        return {
            "kind": "target_reobservation_started",
            "schema": "merlin.target-reobservation-started.v1",
            "wave_kind": "objective_prerequisite",
            "campaign_id": CAMPAIGN_ID,
            "session_id": SESSION_ID,
            "target_id": TARGET_ID,
            "target_session_id": TARGET_SESSION_ID,
            "generation_id": generation.generation_id,
            "obligation_id": generation.obligation_id,
            "event_digests": list(generation.event_digests),
            "event_set_digest": event_set_digest,
            "observation_cutoff_sequence": generation.observation_cutoff_sequence,
            "source_wave": generation.source_wave,
            "attempt": attempt,
            "schedule_id": schedule_id
            or target_reobservation_schedule_id(CAMPAIGN_ID, generation.generation_id, attempt),
        }

    rows.append({"payload": started_payload(1, schedule_id="00000000-0000-0000-0000-000000000000")})
    foreign = project_target_reobservations(rows)
    assert foreign.active_schedule(generation.generation_id) is None
    assert foreign.generations == {}

    first_schedule_id = target_reobservation_schedule_id(CAMPAIGN_ID, generation.generation_id, 1)
    rows.append({"payload": started_payload(1)})
    started = project_target_reobservations(rows)
    assert started.active_schedule(generation.generation_id) == first_schedule_id
    assert started.eligible(generation.generation_id) is False

    rows.append(
        {
            "payload": {
                "kind": "target_reobservation_blocked",
                "schema": "merlin.target-reobservation-blocked.v1",
                "campaign_id": CAMPAIGN_ID,
                "session_id": SESSION_ID,
                "target_id": TARGET_ID,
                "target_session_id": TARGET_SESSION_ID,
                "generation_id": generation.generation_id,
                "obligation_id": generation.obligation_id,
                "event_digests": generation.event_digests,
                "event_count": len(generation.event_digests),
                "event_set_digest": event_set_digest,
                "observation_cutoff_sequence": generation.observation_cutoff_sequence,
                "source_wave": generation.source_wave,
                "attempt": 1,
                "schedule_id": first_schedule_id,
            }
        }
    )
    blocked = project_target_reobservations(rows)
    assert blocked.active_schedule(generation.generation_id) is None
    assert blocked.eligible(generation.generation_id) is False
    assert blocked.recovery_state(latest_wave=1) == {
        "schema": "merlin.target-reobservation-recovery-state.v1",
        "phase": "reobserve",
        "generation_id": generation.generation_id,
        "target_session_id": TARGET_SESSION_ID,
        "event_set_digest": event_set_digest,
        "source_wave": generation.source_wave,
        "schedule_id": first_schedule_id,
        "settlement_id": None,
        "lead_decision_id": None,
    }

    rows.append(
        {
            "payload": {
                "kind": "target_reobservation_retry_authorized",
                "schema": "merlin.target-reobservation-retry-authorized.v1",
                "campaign_id": CAMPAIGN_ID,
                "session_id": SESSION_ID,
                "target_id": TARGET_ID,
                "target_session_id": TARGET_SESSION_ID,
                "generation_id": generation.generation_id,
                "obligation_id": generation.obligation_id,
                "event_set_digest": event_set_digest,
                "authorized_after_attempt": 1,
                "command_id": "operator-resume-1",
            }
        }
    )
    authorized = project_target_reobservations(rows)
    assert authorized.eligible(generation.generation_id) is True

    rows.append(
        {
            "payload": {
                **rows[-1]["payload"],  # type: ignore[dict-item]
                "command_id": "operator-resume-duplicate",
            }
        }
    )
    duplicate_authorized = project_target_reobservations(rows)
    assert duplicate_authorized.retry_authorizations[generation.generation_id] == {
        1: "operator-resume-1"
    }

    second_schedule_id = target_reobservation_schedule_id(CAMPAIGN_ID, generation.generation_id, 2)
    rows.append({"payload": started_payload(2)})
    retried = project_target_reobservations(rows)
    assert retried.active_schedule(generation.generation_id) == second_schedule_id
    assert retried.eligible(generation.generation_id) is False

    surface_evidence_refs = ["browser-snapshot:current"]
    settled_payload = {
        "kind": "target_reobservation_settled",
        "schema": "merlin.target-reobservation-settlement.v1",
        "campaign_id": CAMPAIGN_ID,
        "session_id": SESSION_ID,
        "target_id": TARGET_ID,
        "target_session_id": TARGET_SESSION_ID,
        "generation_id": generation.generation_id,
        "obligation_id": generation.obligation_id,
        "event_digests": list(generation.event_digests),
        "event_set_digest": event_set_digest,
        "event_count": len(generation.event_digests),
        "observation_cutoff_sequence": generation.observation_cutoff_sequence,
        "source_wave": generation.source_wave,
        "status": "refreshed",
        "attempt": 2,
        "schedule_id": second_schedule_id,
        "settlement_id": "target-reobservation-settled/"
        + generation.generation_id.removeprefix("sha256:"),
        "current_surface_digest": "sha256:" + "6" * 64,
        "surface_evidence_digest": _digest({"evidence_refs": surface_evidence_refs}),
        "surface_evidence_refs": surface_evidence_refs,
        "recon_handoff_id": "handoff-2",
        "compiled_adapter_digest": "sha256:" + "7" * 64,
        "reviewed_adapter_digest": "sha256:" + "8" * 64,
        "result_reference": "task-result:reobserve-2",
        "result_digest": "sha256:" + "9" * 64,
    }
    stale_settlement = {
        **settled_payload,
        "attempt": 1,
        "schedule_id": first_schedule_id,
    }
    rejected_stale = project_target_reobservations([*rows, {"payload": stale_settlement}])
    assert rejected_stale.pending == obligations
    assert rejected_stale.refreshed == {}

    missing_schema = dict(settled_payload)
    missing_schema.pop("schema")
    rejected_schema = project_target_reobservations([*rows, {"payload": missing_schema}])
    assert rejected_schema.pending == obligations
    assert rejected_schema.refreshed == {}

    rows.append({"payload": settled_payload})
    refreshed = project_target_reobservations(rows)
    assert refreshed.pending == ()
    assert refreshed.latest_refreshed == settled_payload

    rows.append(
        {
            "decision_id": "reviewed-but-not-a-lead-decision",
            "payload": {
                "target_reobservation_generations": [
                    {
                        "generation_id": generation.generation_id,
                        "settlement_id": settled_payload["settlement_id"],
                        "target_session_id": TARGET_SESSION_ID,
                    }
                ]
            },
        }
    )
    assert project_target_reobservations(rows).reviewed == {}
    rows.append(
        _lead_decision_row(
            [
                {
                    "generation_id": generation.generation_id,
                    "settlement_id": str(settled_payload["settlement_id"]),
                    "target_session_id": TARGET_SESSION_ID,
                }
            ]
        )
    )
    assert project_target_reobservations(rows).reviewed == {
        generation.generation_id: rows[-1]["decision_id"]
    }


def test_projection_requires_exact_target_session_binding_at_every_lifecycle_stage() -> None:
    obligation = _extract(_receipt(call_id="call-target-session-binding", adapter_stale=False))[0]
    observed = obligation.observed_payload(CAMPAIGN_ID, TARGET_ID)
    wrong_observed = {**observed, "target_session_id": "browser:foreign-context"}
    missing_observed = dict(observed)
    missing_observed.pop("target_session_id")
    assert project_target_reobservations([{"payload": wrong_observed}]).pending == ()
    assert project_target_reobservations([{"payload": missing_observed}]).pending == ()

    rows: list[dict[str, object]] = [{"sequence": 1, "payload": observed}]
    generation = project_target_reobservations(rows).current_generation
    assert generation is not None
    event_set_digest = _digest({"event_digests": generation.event_digests})
    schedule_id = target_reobservation_schedule_id(CAMPAIGN_ID, generation.generation_id, 1)
    start = {
        "kind": "target_reobservation_started",
        "schema": "merlin.target-reobservation-started.v1",
        "wave_kind": "objective_prerequisite",
        "campaign_id": CAMPAIGN_ID,
        "session_id": SESSION_ID,
        "target_id": TARGET_ID,
        "target_session_id": TARGET_SESSION_ID,
        "generation_id": generation.generation_id,
        "obligation_id": generation.obligation_id,
        "event_digests": list(generation.event_digests),
        "event_set_digest": event_set_digest,
        "observation_cutoff_sequence": generation.observation_cutoff_sequence,
        "source_wave": generation.source_wave,
        "attempt": 1,
        "schedule_id": schedule_id,
    }
    wrong_start = {**start, "target_session_id": "browser:foreign-context"}
    assert project_target_reobservations([*rows, {"payload": wrong_start}]).starts == {}
    rows.append({"sequence": 2, "payload": start})

    blocked = {
        "kind": "target_reobservation_blocked",
        "schema": "merlin.target-reobservation-blocked.v1",
        "campaign_id": CAMPAIGN_ID,
        "session_id": SESSION_ID,
        "target_id": TARGET_ID,
        "target_session_id": TARGET_SESSION_ID,
        "generation_id": generation.generation_id,
        "obligation_id": generation.obligation_id,
        "event_digests": list(generation.event_digests),
        "event_count": len(generation.event_digests),
        "event_set_digest": event_set_digest,
        "observation_cutoff_sequence": generation.observation_cutoff_sequence,
        "source_wave": generation.source_wave,
        "attempt": 1,
        "schedule_id": schedule_id,
    }
    wrong_blocked = {**blocked, "target_session_id": "browser:foreign-context"}
    wrong_blocked_projection = project_target_reobservations([*rows, {"payload": wrong_blocked}])
    assert wrong_blocked_projection.blocked == frozenset()
    assert wrong_blocked_projection.active_schedule(generation.generation_id) == schedule_id

    retry = {
        "kind": "target_reobservation_retry_authorized",
        "schema": "merlin.target-reobservation-retry-authorized.v1",
        "campaign_id": CAMPAIGN_ID,
        "session_id": SESSION_ID,
        "target_id": TARGET_ID,
        "target_session_id": TARGET_SESSION_ID,
        "generation_id": generation.generation_id,
        "obligation_id": generation.obligation_id,
        "event_set_digest": event_set_digest,
        "authorized_after_attempt": 1,
        "command_id": "operator-resume-target-session",
    }
    wrong_retry = {**retry, "target_session_id": "browser:foreign-context"}
    wrong_retry_projection = project_target_reobservations(
        [*rows, {"payload": blocked}, {"payload": wrong_retry}]
    )
    assert wrong_retry_projection.retry_authorizations.get(generation.generation_id, {}) == {}

    evidence_refs = ["browser-snapshot:target-session-current"]
    settlement_id = "target-reobservation-settled/" + generation.generation_id.removeprefix(
        "sha256:"
    )
    settlement = {
        "kind": "target_reobservation_settled",
        "schema": "merlin.target-reobservation-settlement.v1",
        "campaign_id": CAMPAIGN_ID,
        "session_id": SESSION_ID,
        "target_id": TARGET_ID,
        "target_session_id": TARGET_SESSION_ID,
        "generation_id": generation.generation_id,
        "obligation_id": generation.obligation_id,
        "event_digests": list(generation.event_digests),
        "event_set_digest": event_set_digest,
        "event_count": len(generation.event_digests),
        "observation_cutoff_sequence": generation.observation_cutoff_sequence,
        "source_wave": generation.source_wave,
        "status": "refreshed",
        "attempt": 1,
        "schedule_id": schedule_id,
        "settlement_id": settlement_id,
        "current_surface_digest": "sha256:" + "6" * 64,
        "surface_evidence_digest": _digest({"evidence_refs": evidence_refs}),
        "surface_evidence_refs": evidence_refs,
        "recon_handoff_id": "handoff-target-session",
        "compiled_adapter_digest": "sha256:" + "7" * 64,
        "reviewed_adapter_digest": "sha256:" + "8" * 64,
        "result_reference": "task-result:target-session",
        "result_digest": "sha256:" + "9" * 64,
    }
    wrong_settlement = {**settlement, "target_session_id": "browser:foreign-context"}
    assert project_target_reobservations([*rows, {"payload": wrong_settlement}]).refreshed == {}

    settled_rows = [*rows, {"sequence": 3, "payload": settlement}]
    wrong_review = _lead_decision_row(
        [
            {
                "generation_id": generation.generation_id,
                "settlement_id": settlement_id,
                "target_session_id": "browser:foreign-context",
            }
        ]
    )
    assert project_target_reobservations([*settled_rows, wrong_review]).reviewed == {}
    valid_review = _lead_decision_row(
        [
            {
                "generation_id": generation.generation_id,
                "settlement_id": settlement_id,
                "target_session_id": TARGET_SESSION_ID,
            }
        ]
    )
    assert project_target_reobservations([*settled_rows, valid_review]).reviewed == {
        generation.generation_id: valid_review["decision_id"]
    }


def test_lead_projection_drains_more_than_sixteen_unreviewed_generations() -> None:
    rows: list[dict[str, object]] = []
    settlements: list[tuple[str, str]] = []
    surface_evidence_refs = ["browser-snapshot:current"]

    for index in range(17):
        obligation = _extract(_receipt(call_id=f"call-lead-batch-{index}", adapter_stale=False))[0]
        rows.append({"payload": obligation.observed_payload(CAMPAIGN_ID, TARGET_ID)})
        generation = project_target_reobservations(rows).current_generation
        assert generation is not None
        event_set_digest = _digest({"event_digests": generation.event_digests})
        schedule_id = target_reobservation_schedule_id(CAMPAIGN_ID, generation.generation_id, 1)
        settlement_id = "target-reobservation-settled/" + generation.generation_id.removeprefix(
            "sha256:"
        )
        rows.extend(
            (
                {
                    "payload": {
                        "kind": "target_reobservation_started",
                        "schema": "merlin.target-reobservation-started.v1",
                        "wave_kind": "objective_prerequisite",
                        "campaign_id": CAMPAIGN_ID,
                        "session_id": SESSION_ID,
                        "target_id": TARGET_ID,
                        "target_session_id": TARGET_SESSION_ID,
                        "generation_id": generation.generation_id,
                        "obligation_id": generation.obligation_id,
                        "event_digests": list(generation.event_digests),
                        "event_set_digest": event_set_digest,
                        "observation_cutoff_sequence": generation.observation_cutoff_sequence,
                        "source_wave": generation.source_wave,
                        "attempt": 1,
                        "schedule_id": schedule_id,
                    }
                },
                {
                    "payload": {
                        "kind": "target_reobservation_settled",
                        "schema": "merlin.target-reobservation-settlement.v1",
                        "campaign_id": CAMPAIGN_ID,
                        "session_id": SESSION_ID,
                        "target_id": TARGET_ID,
                        "target_session_id": TARGET_SESSION_ID,
                        "generation_id": generation.generation_id,
                        "obligation_id": generation.obligation_id,
                        "event_digests": list(generation.event_digests),
                        "event_set_digest": event_set_digest,
                        "event_count": len(generation.event_digests),
                        "observation_cutoff_sequence": generation.observation_cutoff_sequence,
                        "source_wave": generation.source_wave,
                        "status": "refreshed",
                        "attempt": 1,
                        "schedule_id": schedule_id,
                        "settlement_id": settlement_id,
                        "current_surface_digest": "sha256:" + "6" * 64,
                        "surface_evidence_digest": _digest(
                            {"evidence_refs": surface_evidence_refs}
                        ),
                        "surface_evidence_refs": surface_evidence_refs,
                        "recon_handoff_id": f"handoff-{index}",
                        "compiled_adapter_digest": "sha256:" + "7" * 64,
                        "reviewed_adapter_digest": "sha256:" + "8" * 64,
                        "result_reference": f"task-result:reobserve-{index}",
                        "result_digest": "sha256:" + "9" * 64,
                    }
                },
            )
        )
        settlements.append((generation.generation_id, settlement_id))

    projected = project_target_reobservations(rows)
    assert len(projected.review_pending) == 17
    first_batch = projected.lead_projection(latest_wave=1)["refreshed"]
    assert [item["generation_id"] for item in first_batch] == [
        generation_id for generation_id, _settlement_id in settlements[:16]
    ]

    rows.append(
        _lead_decision_row(
            [
                {
                    "generation_id": generation_id,
                    "settlement_id": settlement_id,
                    "target_session_id": TARGET_SESSION_ID,
                }
                for generation_id, settlement_id in settlements[:16]
            ]
        )
    )
    projected = project_target_reobservations(rows)
    assert len(projected.review_pending) == 1
    assert [
        item["generation_id"] for item in projected.lead_projection(latest_wave=1)["refreshed"]
    ] == [settlements[-1][0]]

    rows.append(
        _lead_decision_row(
            [
                {
                    "generation_id": settlements[-1][0],
                    "settlement_id": settlements[-1][1],
                    "target_session_id": TARGET_SESSION_ID,
                }
            ]
        )
    )
    projected = project_target_reobservations(rows)
    assert projected.review_pending == ()
    reviewed_context = projected.lead_projection(latest_wave=1)["refreshed"]
    assert len(reviewed_context) == 16
    assert reviewed_context[-1]["generation_id"] == settlements[-1][0]


def test_projection_rejects_subset_initial_generation_and_attempt_gaps() -> None:
    obligations = extract_target_reobservation_obligations(
        campaign_id=CAMPAIGN_ID,
        session_id=SESSION_ID,
        target_id=TARGET_ID,
        source_schedule_id="schedule-1",
        source_wave=1,
        task_ids=[TASK_ID],
        evidence={
            TASK_ID: {
                "target_evidence_receipts": {
                    call_id: _receipt(call_id=call_id, adapter_stale=False)
                    for call_id in ("call-subset-1", "call-subset-2")
                }
            }
        },
    )
    rows: list[dict[str, object]] = [
        {
            "sequence": index,
            "payload": obligation.observed_payload(CAMPAIGN_ID, TARGET_ID),
        }
        for index, obligation in enumerate(obligations, 1)
    ]
    subset = project_target_reobservations(rows[:1]).current_generation
    assert subset is not None

    def start_payload(generation, attempt: int) -> dict[str, object]:
        return {
            "kind": "target_reobservation_started",
            "schema": "merlin.target-reobservation-started.v1",
            "wave_kind": "objective_prerequisite",
            "campaign_id": CAMPAIGN_ID,
            "session_id": SESSION_ID,
            "target_id": TARGET_ID,
            "target_session_id": generation.target_session_id,
            "generation_id": generation.generation_id,
            "obligation_id": generation.obligation_id,
            "event_digests": list(generation.event_digests),
            "event_set_digest": _digest({"event_digests": generation.event_digests}),
            "observation_cutoff_sequence": generation.observation_cutoff_sequence,
            "source_wave": generation.source_wave,
            "attempt": attempt,
            "schedule_id": target_reobservation_schedule_id(
                CAMPAIGN_ID, generation.generation_id, attempt
            ),
        }

    subset_rows = [*rows, {"sequence": 3, "payload": start_payload(subset, 1)}]
    subset_projection = project_target_reobservations(subset_rows)
    assert subset_projection.generations == {}
    assert subset_projection.starts == {}

    complete = project_target_reobservations(rows).current_generation
    assert complete is not None
    attempt_gap = project_target_reobservations(
        [*rows, {"sequence": 3, "payload": start_payload(complete, 2)}]
    )
    assert attempt_gap.generations == {}
    assert attempt_gap.starts == {}


def test_duplicate_retry_authorization_cannot_preapprove_later_attempt() -> None:
    obligation = _extract(_receipt(call_id="call-sequential", adapter_stale=False))[0]
    rows: list[dict[str, object]] = [
        {"sequence": 1, "payload": obligation.observed_payload(CAMPAIGN_ID, TARGET_ID)}
    ]
    generation = project_target_reobservations(rows).current_generation
    assert generation is not None
    event_set_digest = _digest({"event_digests": generation.event_digests})

    def start(attempt: int) -> dict[str, object]:
        return {
            "kind": "target_reobservation_started",
            "schema": "merlin.target-reobservation-started.v1",
            "wave_kind": "objective_prerequisite",
            "campaign_id": CAMPAIGN_ID,
            "session_id": SESSION_ID,
            "target_id": TARGET_ID,
            "target_session_id": generation.target_session_id,
            "generation_id": generation.generation_id,
            "obligation_id": generation.obligation_id,
            "event_digests": generation.event_digests,
            "event_set_digest": event_set_digest,
            "observation_cutoff_sequence": generation.observation_cutoff_sequence,
            "source_wave": generation.source_wave,
            "attempt": attempt,
            "schedule_id": target_reobservation_schedule_id(
                CAMPAIGN_ID, generation.generation_id, attempt
            ),
        }

    def blocked(attempt: int) -> dict[str, object]:
        return {
            "kind": "target_reobservation_blocked",
            "schema": "merlin.target-reobservation-blocked.v1",
            "campaign_id": CAMPAIGN_ID,
            "session_id": SESSION_ID,
            "target_id": TARGET_ID,
            "target_session_id": generation.target_session_id,
            "generation_id": generation.generation_id,
            "obligation_id": generation.obligation_id,
            "event_digests": generation.event_digests,
            "event_count": len(generation.event_digests),
            "event_set_digest": event_set_digest,
            "observation_cutoff_sequence": generation.observation_cutoff_sequence,
            "source_wave": generation.source_wave,
            "attempt": attempt,
            "schedule_id": target_reobservation_schedule_id(
                CAMPAIGN_ID, generation.generation_id, attempt
            ),
        }

    def authorize(attempt: int, command: str) -> dict[str, object]:
        return {
            "kind": "target_reobservation_retry_authorized",
            "schema": "merlin.target-reobservation-retry-authorized.v1",
            "campaign_id": CAMPAIGN_ID,
            "session_id": SESSION_ID,
            "target_id": TARGET_ID,
            "target_session_id": generation.target_session_id,
            "generation_id": generation.generation_id,
            "obligation_id": generation.obligation_id,
            "event_set_digest": event_set_digest,
            "authorized_after_attempt": attempt,
            "command_id": command,
        }

    rows.extend(
        {"payload": payload}
        for payload in (
            start(1),
            blocked(1),
            authorize(1, "resume-one"),
            authorize(1, "resume-one-duplicate"),
            start(2),
            blocked(2),
        )
    )
    projected = project_target_reobservations(rows)
    assert projected.retry_authorizations[generation.generation_id] == {1: "resume-one"}
    assert projected.eligible(generation.generation_id) is False

    forged_third = project_target_reobservations([*rows, {"payload": start(3)}])
    assert len(forged_third.starts[generation.generation_id]) == 2
    assert forged_third.active_schedule(generation.generation_id) is None

    rows.append({"payload": authorize(2, "resume-two")})
    authorized = project_target_reobservations(rows)
    assert authorized.retry_authorizations[generation.generation_id] == {
        1: "resume-one",
        2: "resume-two",
    }
    assert authorized.eligible(generation.generation_id) is True


def test_pending_event_overflow_stays_in_typed_recovery_barrier() -> None:
    receipts = {
        f"call-overflow-{index}": _receipt(call_id=f"call-overflow-{index}", adapter_stale=False)
        for index in range(257)
    }
    obligations = extract_target_reobservation_obligations(
        campaign_id=CAMPAIGN_ID,
        session_id=SESSION_ID,
        target_id=TARGET_ID,
        source_schedule_id="schedule-overflow",
        source_wave=3,
        task_ids=[TASK_ID],
        evidence={TASK_ID: {"target_evidence_receipts": receipts}},
    )
    other_target_session = _extract(
        _receipt(
            call_id="call-overflow-other-context",
            adapter_stale=False,
            target_session_id="browser:context-2",
        )
    )
    projection = project_target_reobservations(
        [
            {
                "sequence": index,
                "payload": obligation.observed_payload(CAMPAIGN_ID, TARGET_ID),
            }
            for index, obligation in enumerate((*obligations, *other_target_session), 1)
        ]
    )

    assert len(projection.pending) == 258
    assert projection.current_generation is None
    assert projection.overflow is not None
    assert projection.overflow.event_count == 257
    assert projection.overflow.target_session_id == TARGET_SESSION_ID
    assert projection.lead_projection()["generation"]["status"] == "blocked_event_overflow"  # type: ignore[index]
    recovery = projection.recovery_state(latest_wave=3)
    assert recovery is not None
    assert recovery["phase"] == "reobserve"
    assert recovery["generation_id"] == projection.overflow.generation_id
    assert recovery["event_set_digest"] == projection.overflow.event_set_digest
    assert recovery["schedule_id"] is None
