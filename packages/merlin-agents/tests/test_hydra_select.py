from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, replace

import pytest
from merlin_agents.hydra_select import (
    HydraPlan,
    HydraSelectContract,
    HydraSelectState,
    HydraStep,
)
from merlin_contracts import HYDRA_SELECT_CATALOG_DIGEST, trusted_hydra_select_mechanisms


def digest(value: object) -> str:
    return (
        "sha256:"
        + hashlib.sha256(
            json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
    )


def contract(*, goal: str = "goal") -> HydraSelectContract:
    return HydraSelectContract(
        version="merlin.hydra-select.v1",
        semantic_technique_id="semantic:hydra",
        goal_digest=digest(goal),
        objective_oracle_digest=digest("oracle"),
        reviewed_adapter_digest=digest("adapter"),
        attacker_route_digest=digest("route"),
        attacker_tool_transport="buffered",
        target_profile_id="target-1",
        session_policy="same-reviewed-session",
        modality="text",
        language="en",
        mechanisms=trusted_hydra_select_mechanisms(),
        mechanism_catalog_digest=HYDRA_SELECT_CATALOG_DIGEST,
        proposed_count=6,
        selected_count=3,
        max_candidate_chars=4096,
        stop_on_positive=True,
    )


def proposal(*, duplicate: bool = False) -> dict[str, object]:
    return {
        "candidates": [
            {
                "prompt": "candidate 0" if duplicate and index == 5 else f"candidate {index}",
                "rationale": f"low-trust rationale {index}",
            }
            for index in range(6)
        ]
    }


def test_selector_retains_duplicate_disposition_and_never_selects_it() -> None:
    plan = HydraPlan.parse(proposal(duplicate=True), contract())

    duplicates = [item for item in plan.candidates if item.disposition == "duplicate"]
    assert len(duplicates) == 1
    assert duplicates[0].duplicate_of is not None
    assert duplicates[0].candidate_id not in plan.selected_candidate_ids
    assert len(plan.selected_candidate_ids) == 3


def test_selector_is_deterministic_and_ignores_rationale() -> None:
    first = HydraPlan.parse(proposal(), contract())
    changed = proposal()
    for item in changed["candidates"]:  # type: ignore[index]
        item["rationale"] = "different attacker judgment"  # type: ignore[index]
    second = HydraPlan.parse(changed, contract())

    assert first.selected_candidate_ids == second.selected_candidate_ids
    assert [item.rank for item in first.candidates] == [item.rank for item in second.candidates]


def test_catalogue_diversity_sampling_does_not_starve_later_slots() -> None:
    selected_mechanisms = {
        item.requested_mechanism_id
        for goal_index in range(16)
        for item in HydraPlan.parse(proposal(), contract(goal=f"goal-{goal_index}")).selected()
    }

    assert selected_mechanisms == {item[0] for item in trusted_hydra_select_mechanisms()}


def test_restore_rederives_plan_and_rejects_tampered_rank() -> None:
    state = HydraSelectState.start(contract(), "attempt-1").with_plan(contract(), proposal())
    stored = state.dump()
    stored["plan"]["candidates"][0]["rank"] = 999  # type: ignore[index]

    with pytest.raises(ValueError, match="selection plan drifted"):
        HydraSelectState.restore(stored, contract(), "attempt-1")


def test_malformed_or_insufficient_distinct_batch_has_zero_plan() -> None:
    all_same = {
        "candidates": [{"prompt": "same", "rationale": f"reason {index}"} for index in range(6)]
    }
    duplicate_plan = HydraPlan.parse(all_same, contract())

    extra = proposal()
    extra["candidates"][0]["score"] = 1  # type: ignore[index]
    telemetry_plan = HydraPlan.parse(extra, contract())

    assert len(duplicate_plan.selected_candidate_ids) == 1
    assert duplicate_plan.evaluation_uncertainties
    assert len(telemetry_plan.selected_candidate_ids) == contract().selected_count
    assert telemetry_plan.evaluation_uncertainties


def test_parser_retains_valid_candidate_subset_and_records_missing_diversity() -> None:
    partial = proposal()["candidates"][:3]  # type: ignore[index]
    partial[1] = {"prompt": ""}
    plan = HydraPlan.parse({"candidates": partial}, contract())

    assert len(plan.candidates) == 2
    assert len(plan.selected_candidate_ids) == 2
    assert any("diversity" in str(item["reason"]) for item in plan.evaluation_uncertainties)
    state = HydraSelectState.start(contract(), "attempt-1").with_plan(
        contract(), {"candidates": partial}
    )
    assert HydraSelectState.restore(state.dump(), contract(), "attempt-1") == state


def test_contract_accepts_explicit_multimodal_language_cell() -> None:
    review = replace(contract(), modality="image", language="lzh", proposed_count=4)

    assert review.modality == "image"
    assert review.language == "lzh"


def test_dump_is_json_safe_and_candidate_ids_are_unique() -> None:
    state = HydraSelectState.start(contract(), "attempt-1").with_plan(contract(), proposal())
    assert len({item.candidate_id for item in state.plan.candidates}) == 6  # type: ignore[union-attr]
    json.dumps(asdict(state))


def test_restore_accepts_observation_only_state_after_selected_candidates() -> None:
    review = contract()
    state = HydraSelectState.start(review, "attempt-1").with_plan(review, proposal())
    steps = tuple(
        HydraStep(
            index=index,
            scheduler_attempt_id="attempt-1",
            candidate_id=candidate_id,
            call_id=f"call-{index}",
            arguments_digest=digest({"input": index}),
            exchange_digest=digest({"exchange": index}),
            exchange_id=f"exchange-{index}",
            objective_observation_digest=digest({"observation": index}),
            objective_matched=False,
            target_reply_digest=digest({"reply": index}),
            tool_content=f"reply {index}",
        )
        for index, candidate_id in enumerate(state.plan.selected_candidate_ids)  # type: ignore[union-attr]
    )
    stored = replace(state, target_session_id="chat-1", steps=steps).dump()

    restored = HydraSelectState.restore(stored, review, "attempt-1")

    assert restored.terminal_reason is None
    assert len(restored.steps) == review.selected_count


def test_acknowledged_effect_survives_missing_oracle_metadata() -> None:
    review = contract()
    state = HydraSelectState.start(review, "attempt-1").with_plan(review, proposal())
    arguments = state.next_arguments()
    advanced = state.with_pending("call-0").advance(
        review,
        target_session_id="chat-1",
        call_id="call-0",
        arguments=arguments,
        tool_content="target reply",
        exchange={"response": "target reply"},
    )

    assert len(advanced.steps) == 1
    assert advanced.steps[0].objective_matched is None
    assert advanced.terminal_reason is None
    assert any(item["kind"] == "evaluation_uncertain" for item in advanced.evaluation_uncertainties)
