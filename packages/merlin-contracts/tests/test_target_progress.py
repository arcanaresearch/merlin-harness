from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime

from merlin_contracts import (
    ToolEffectRecord,
    ToolEffectState,
    build_state_observation,
    evaluate_state_transition,
    project_target_progress,
)


def _observation(name: str, role: str, phase: str, value: object) -> dict[str, object]:
    return build_state_observation(
        name=name,
        role=role,
        phase=phase,
        step_id=f"{phase}-state",
        evaluation_root="response_body",
        pointer=f"/state/{name}",
        binding_context={"adapter": "reviewed"},
        selected_value=value,
        value_type="string" if isinstance(value, str) else "integer",
        preferred_direction="increase" if role in {"score", "progress_ordinal"} else None,
        minimum_value=0 if isinstance(value, int) else None,
        maximum_value=100 if role == "score" else None,
        trust_class="host_observed",
    )


def _effect(call_id: str, observations: list[dict[str, object]]) -> ToolEffectRecord:
    return ToolEffectRecord(
        idempotency_key=f"effect:{call_id}",
        session_id="session/1",
        call_id=call_id,
        campaign_id="campaign/1",
        target_id="target/1",
        tool_name="target_request",
        effect="target_network",
        arguments_digest="sha256:" + "1" * 64,
        state=ToolEffectState.ACKNOWLEDGED,
        result_metadata={
            "state_observations": observations,
            "target_state_transition": evaluate_state_transition(observations),
            "current_target_exchanges": [{"interaction": True}],
        },
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
        updated_at=datetime(2026, 1, 1, tzinfo=UTC),
    )


def test_target_progress_projects_current_partition_and_transition() -> None:
    observations = [
        _observation("run_id", "state_identity", "baseline", "run-a"),
        _observation("run_id", "state_identity", "result", "run-a"),
        _observation("tier", "progress_ordinal", "baseline", 1),
        _observation("tier", "progress_ordinal", "result", 2),
        _observation("score", "score", "baseline", 90),
        _observation("score", "score", "result", 100),
    ]

    snapshot = project_target_progress(
        [_effect("call/1", observations)],
        campaign_id="campaign/1",
        target_id="target/1",
        now=datetime(2026, 1, 1, 0, 1, tzinfo=UTC),
    )

    assert snapshot["status"] == "observed"
    assert snapshot["freshness"] == "current"
    assert snapshot["target_exchange_count"] == 1
    assert [(item["name"], item["value"]) for item in snapshot["current_state"]] == [
        ("tier", 2),
        ("score", 100),
    ]
    assert snapshot["latest_transition"]["outcome"] == "progress"


def test_target_progress_deduplicates_promoted_browser_observations() -> None:
    level = _observation("level", "progress_ordinal", "result", 2)
    effect = _effect("call/browser", [level])
    metadata = dict(effect.result_metadata)
    metadata["target_exchanges"] = [
        {
            "interaction": True,
            "state_observations": [level],
            "target_state_transition": metadata["target_state_transition"],
        }
    ]
    effect = effect.model_copy(update={"result_metadata": metadata})

    snapshot = project_target_progress(
        [effect],
        campaign_id="campaign/1",
        target_id="target/1",
        now=datetime(2026, 1, 1, 0, 1, tzinfo=UTC),
    )

    assert snapshot["target_exchange_count"] == 1
    assert [(item["name"], item["value"]) for item in snapshot["current_state"]] == [("level", 2)]


def test_target_progress_is_explicit_when_state_contract_has_no_observations() -> None:
    effect = _effect("call/1", [])

    snapshot = project_target_progress(
        [effect],
        campaign_id="campaign/1",
        target_id="target/1",
        now=datetime(2026, 1, 1, tzinfo=UTC),
    )

    assert snapshot["status"] == "unknown"
    assert snapshot["target_exchange_count"] == 1
    assert snapshot["current_state"] == ()
    assert "not been discovered" in snapshot["limitations"][0]


def test_target_progress_discloses_a_bounded_history_window() -> None:
    snapshot = project_target_progress(
        [_effect("call/1", [])],
        campaign_id="campaign/1",
        target_id="target/1",
        now=datetime(2026, 1, 1, tzinfo=UTC),
        history_truncated=True,
    )

    assert snapshot["history_truncated"] is True
    assert "outside this live projection window" in snapshot["limitations"][-1]


def test_target_progress_revision_does_not_change_as_evidence_ages() -> None:
    observations = [_observation("tier", "progress_ordinal", "result", 2)]

    current = project_target_progress(
        [_effect("call/1", observations)],
        campaign_id="campaign/1",
        target_id="target/1",
        now=datetime(2026, 1, 1, 0, 1, tzinfo=UTC),
    )
    stale = project_target_progress(
        [_effect("call/1", observations)],
        campaign_id="campaign/1",
        target_id="target/1",
        now=datetime(2026, 1, 2, tzinfo=UTC),
    )

    assert current["snapshot_revision"] == stale["snapshot_revision"]
    assert current["freshness"] == "current"
    assert stale["freshness"] == "stale"


def test_target_progress_rejects_invalid_selected_value_digest() -> None:
    observation = _observation("tier", "progress_ordinal", "result", 2)
    observation["selected_digest"] = "sha256:" + "0" * 64
    unsigned = dict(observation)
    unsigned.pop("observation_digest")
    observation["observation_digest"] = (
        "sha256:"
        + hashlib.sha256(
            json.dumps(unsigned, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
    )

    snapshot = project_target_progress(
        [_effect("call/1", [observation])],
        campaign_id="campaign/1",
        target_id="target/1",
        now=datetime(2026, 1, 1, tzinfo=UTC),
    )

    assert snapshot["status"] == "unknown"
    assert "invalid state observation" in snapshot["limitations"][-1]
