from __future__ import annotations

import hashlib
import json
from dataclasses import replace

import pytest
from merlin_agents.crescendo import CrescendoContract, CrescendoState


def digest(value: object) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(payload.encode()).hexdigest()


def contract(*, plateau_turns: int = 2, max_target_turns: int = 5) -> CrescendoContract:
    return CrescendoContract(
        version="merlin.crescendo.v1",
        semantic_technique_id="technique:crescendo",
        objective_oracle_digest=digest("objective"),
        refusal_oracle_digest=digest("refusal"),
        progress_oracle_digest=digest("progress"),
        invalid_premise_oracle_digest=digest("invalid"),
        reviewed_adapter_digest=digest("adapter"),
        modality="text",
        language="en",
        max_target_turns=max_target_turns,
        plateau_turns=plateau_turns,
    )


def exchange(review: CrescendoContract, *, matched: str | None = None) -> dict[str, object]:
    purposes = {
        "objective": review.objective_oracle_digest,
        "refusal": review.refusal_oracle_digest,
        "progress": review.progress_oracle_digest,
        "invalid-premise": review.invalid_premise_oracle_digest,
    }
    observations = []
    for purpose, oracle_digest in purposes.items():
        binding = {
            "adapter_digest": review.reviewed_adapter_digest,
            "step_id": "interaction",
            "oracle_digest": oracle_digest,
            "pointer": "/result",
            "match_values": [purpose],
            "modality": "text",
            "turn": "multi",
            "language": "en",
            "purpose": purpose,
        }
        observation = {
            "schema": "merlin.api-oracle-observation.v2",
            **binding,
            "binding_digest": digest(binding),
            "matched": purpose == matched,
            "observed_value_digest": digest(purpose),
        }
        observation["observation_digest"] = digest(observation)
        observations.append(observation)
    return {"oracle_observations": observations}


def advance(state: CrescendoState, review: CrescendoContract, matched: str | None):
    return state.advance(
        review,
        target_session_id="api:campaign",
        prompt_arguments_digest=digest(f"prompt-{len(state.steps)}"),
        exchange=exchange(review, matched=matched),
    )


def test_contract_accepts_explicit_multimodal_language_cell() -> None:
    review = replace(contract(), modality="document", language="lzh")

    assert review.modality == "document"
    assert review.language == "lzh"


def test_refusal_backtracks_without_consuming_plateau_quota() -> None:
    review = contract(plateau_turns=1)
    state = advance(CrescendoState.start(review, "attempt-1"), review, "refusal")
    assert state.next_stage == "backtrack"
    assert state.no_progress_turns == 0
    assert state.terminal_reason is None


@pytest.mark.parametrize(
    ("matched", "terminal"),
    [("objective", "positive"), ("invalid-premise", "invalid-premise")],
)
def test_daemon_observation_is_terminal_authority(matched: str, terminal: str) -> None:
    review = contract()
    state = advance(CrescendoState.start(review, "attempt-1"), review, matched)
    assert state.terminal_reason is None
    assert state.steps[-1].decision == ("success" if terminal == "positive" else "invalid-premise")
    assert CrescendoState.restore(state.dump(), review, "attempt-1") == state


def test_only_qualified_no_progress_causes_plateau() -> None:
    review = contract(plateau_turns=2)
    state = advance(CrescendoState.start(review, "attempt-1"), review, None)
    assert state.terminal_reason is None
    state = advance(state, review, None)
    assert state.terminal_reason is None
    assert state.no_progress_turns == 2


def test_restore_rejects_forged_transition_history() -> None:
    review = contract()
    state = advance(CrescendoState.start(review, "attempt-1"), review, "objective")
    observation_only = state.dump()
    observation_only["terminal_reason"] = None

    restored = CrescendoState.restore(observation_only, review, "attempt-1")

    assert restored.terminal_reason is None
    assert restored.steps[-1].decision == "success"


def test_observation_from_another_adapter_cannot_advance() -> None:
    review = contract()
    payload = exchange(review, matched="progress")
    observation = payload["oracle_observations"][0]  # type: ignore[index]
    observation["adapter_digest"] = digest("foreign")  # type: ignore[index]
    unsigned = {key: value for key, value in observation.items() if key != "observation_digest"}  # type: ignore[union-attr]
    observation["observation_digest"] = digest(unsigned)  # type: ignore[index]
    advanced = CrescendoState.start(review, "attempt-1").advance(
        review,
        target_session_id="api:campaign",
        prompt_arguments_digest=digest("prompt"),
        exchange=payload,
    )

    assert advanced.steps[-1].decision == "uncertain"
    assert any(
        "integrity" in str(item["reason"]) or "adapter_digest" in str(item["reason"])
        for item in advanced.evaluation_uncertainties
    )
