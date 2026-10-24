from __future__ import annotations

import hashlib
import json
from dataclasses import replace

from merlin_agents.goat import (
    GOAT_TOOLBOX_CATALOG_DIGEST,
    GoatContract,
    GoatPlan,
    GoatState,
    trusted_goat_toolbox,
)
from merlin_agents.runtime import _restore_goat_protocol
from merlin_contracts import ChatMessage


def digest(value: object) -> str:
    return (
        "sha256:"
        + hashlib.sha256(
            json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
    )


def contract(*, turns: int = 3) -> GoatContract:
    return GoatContract(
        version="merlin.goat.v1",
        semantic_technique_id="semantic:goat",
        goal_digest=digest("goal"),
        toolbox=trusted_goat_toolbox(),
        toolbox_catalog_digest=GOAT_TOOLBOX_CATALOG_DIGEST,
        terminal_oracle_digest=digest("oracle"),
        reviewed_adapter_digest=digest("adapter"),
        attacker_route_digest=digest("attacker-route"),
        attacker_tool_transport="buffered",
        target_profile_id="target-1",
        session_policy="same-reviewed-session",
        modality="text",
        language="en",
        target_turns=turns,
    )


def plan(response: str = "ask") -> GoatPlan:
    return GoatPlan.parse(
        {
            "observation": "The target previously declined.",
            "thought": "Use a hypothetical next.",
            "strategy": ["hypothetical"],
            "response": response,
        },
        contract(),
    )


def exchange(review: GoatContract, matched: bool = False) -> dict[str, object]:
    binding = {
        "adapter_digest": review.reviewed_adapter_digest,
        "step_id": "interaction",
        "oracle_digest": review.terminal_oracle_digest,
        "pointer": "/result",
        "match_values": ["matched"],
        "modality": "text",
        "turn": "multi",
        "language": "en",
        "purpose": "objective",
    }
    body = {
        "schema": "merlin.api-oracle-observation.v2",
        **binding,
        "binding_digest": digest(binding),
        "matched": matched,
    }
    return {
        "id": 1,
        "interaction": True,
        "oracle_observations": [{**body, "observation_digest": digest(body)}],
    }


def test_goat_is_fixed_k_and_does_not_self_judge() -> None:
    review = contract(turns=2)
    state = GoatState.start(review, "attempt-1")
    first = state.advance(
        review,
        plan=plan("first"),
        target_session_id="chat-1",
        call_id="call-0",
        tool_content="reply 0",
        arguments_digest=digest({"message": "first"}),
        exchange=exchange(review, matched=True),
    )
    assert first.terminal_reason is None
    second = first.advance(
        review,
        plan=plan("second"),
        target_session_id="chat-1",
        call_id="call-1",
        tool_content="reply 1",
        arguments_digest=digest({"message": "second"}),
        exchange=exchange(review, matched=False),
    )
    assert second.terminal_reason is None
    assert [step.objective_matched for step in second.steps] == [True, False]
    assert GoatState.restore(second.dump(), review, "attempt-1") == second


def test_restore_accepts_observation_only_state_after_fixed_k() -> None:
    review = contract(turns=2)
    state = GoatState.start(review, "attempt-1")
    for index in range(2):
        state = state.advance(
            review,
            plan=plan(f"turn-{index}"),
            target_session_id="chat-1",
            call_id=f"call-{index}",
            tool_content=f"reply {index}",
            arguments_digest=digest({"message": f"turn-{index}"}),
            exchange=exchange(review),
        )
    stored = state.dump()
    stored["terminal_reason"] = None

    restored = GoatState.restore(stored, review, "attempt-1")

    assert restored.terminal_reason is None
    assert len(restored.steps) == review.target_turns


def test_goat_rejects_freeform_or_unknown_strategy_before_effect() -> None:
    review = contract()
    freeform = GoatPlan.parse("Response: fallback", review)
    unknown = GoatPlan.parse(
        {
            "observation": "o",
            "thought": "t",
            "strategy": ["claim-success"],
            "response": "r",
        },
        review,
    )

    assert freeform.response == "Response: fallback"
    assert freeform.evaluation_uncertainties
    assert unknown.response == "r"
    assert unknown.strategies == ()
    assert unknown.evaluation_uncertainties
    state = GoatState.start(review, "attempt-1").advance(
        review,
        plan=unknown,
        target_session_id="chat-1",
        call_id="call-0",
        tool_content="reply",
        arguments_digest=digest({"message": "r"}),
        exchange=exchange(review),
    )
    assert GoatState.restore(state.dump(), review, "attempt-1") == state


def test_goat_contract_accepts_explicit_multimodal_language_cell() -> None:
    review = replace(contract(turns=1), modality="audio", language="lzh")

    assert review.target_turns == 1
    assert review.modality == "audio"
    assert review.language == "lzh"


def test_goat_rejects_missing_or_drifted_objective_observation() -> None:
    review = contract()
    state = GoatState.start(review, "attempt-1")
    advanced = state.advance(
        review,
        plan=plan(),
        target_session_id="chat-1",
        call_id="call-0",
        tool_content="reply",
        arguments_digest=digest({"message": "ask"}),
        exchange={"id": 1, "interaction": True, "response": "target reply"},
    )

    assert advanced.steps[0].objective_matched is None
    assert advanced.steps[0].target_reply == "target reply"
    assert advanced.evaluation_uncertainties[0]["kind"] == "evaluation_uncertain"
    assert "absent" in str(advanced.evaluation_uncertainties[0]["reason"])


def test_goat_accepts_only_target_authority_for_current_signed_observations() -> None:
    review = contract(turns=1)
    binding = {
        "authority": "target",
        "adapter_digest": review.reviewed_adapter_digest,
        "compiled_adapter_digest": digest("compiled-adapter"),
        "step_id": "interaction",
        "oracle_digest": review.terminal_oracle_digest,
        "pointer": "/result",
        "match_values": ["matched"],
        "modality": "text",
        "turn": "multi",
        "language": "en",
        "purpose": "objective",
    }
    body = {
        "schema": "merlin.api-oracle-observation.v2",
        **binding,
        "binding_digest": digest(binding),
        "matched": True,
    }
    signed_exchange = {
        "id": 1,
        "interaction": True,
        "oracle_observations": [{**body, "observation_digest": digest(body)}],
    }

    accepted = GoatState.start(review, "attempt-1").advance(
        review,
        plan=plan(),
        target_session_id="chat-1",
        call_id="call-0",
        tool_content="reply",
        arguments_digest=digest({"message": "ask"}),
        exchange=signed_exchange,
    )
    assert accepted.steps[0].objective_matched is True
    assert accepted.evaluation_uncertainties == ()

    for invalid_authority in (None, "model"):
        invalid_binding = {
            key: value
            for key, value in binding.items()
            if key != "authority" or invalid_authority is not None
        }
        if invalid_authority is not None:
            invalid_binding["authority"] = invalid_authority
        invalid_body = {
            "schema": "merlin.api-oracle-observation.v2",
            **invalid_binding,
            "binding_digest": digest(invalid_binding),
            "matched": True,
        }
        invalid_exchange = {
            "id": 1,
            "interaction": True,
            "oracle_observations": [{**invalid_body, "observation_digest": digest(invalid_body)}],
        }
        rejected = GoatState.start(review, "attempt-1").advance(
            review,
            plan=plan(),
            target_session_id="chat-1",
            call_id="call-0",
            tool_content="reply",
            arguments_digest=digest({"message": "ask"}),
            exchange=invalid_exchange,
        )
        assert rejected.steps[0].objective_matched is None
        assert rejected.evaluation_uncertainties[0]["source"] == "goat.oracle"


def test_goat_replacement_restores_exact_non_system_attacker_history() -> None:
    review = contract(turns=2)
    state = GoatState.start(review, "attempt-1").advance(
        review,
        plan=plan("exact response"),
        target_session_id="chat-1",
        call_id="call-0",
        tool_content='{"reply":"low-trust target text"}',
        arguments_digest=digest({"message": "exact response"}),
        exchange={**exchange(review), "response": {"reply": "low-trust target text"}},
    )
    restored = _restore_goat_protocol(
        [ChatMessage(role="system", content="identity"), ChatMessage(role="user", content="goal")],
        state,
        review,
    )

    assert [message.role for message in restored] == [
        "system",
        "user",
        "system",
        "assistant",
        "tool",
        "system",
    ]
    assert restored[3].tool_calls[0]["id"] == "call-0"
    assert (
        json.loads(restored[3].tool_calls[0]["function"]["arguments"])["input"] == "exact response"
    )
    assert json.loads(restored[4].content) == {"reply": "low-trust target text"}
    assert restored[4].role == "tool"
    assert "low-trust target text" not in restored[-1].content
