from __future__ import annotations

import hashlib
import json

import pytest
from merlin_agents.completion_frontier import CompletionCandidateIdentity
from merlin_agents.crescendo import CrescendoContract
from merlin_agents.goat import GoatContract
from merlin_agents.hydra_select import HydraSelectContract
from merlin_agents.strategy_plugin import (
    CrescendoStrategyAdapter,
    DirectStrategyAdapter,
    GoatStrategyAdapter,
    HydraStrategyAdapter,
    StrategyDeviation,
    StrategyProvenance,
    TargetObservation,
    UnavailableStrategyAdapter,
)
from merlin_contracts import (
    GOAT_TOOLBOX_CATALOG_DIGEST,
    HYDRA_SELECT_CATALOG_DIGEST,
    trusted_goat_toolbox,
    trusted_hydra_select_mechanisms,
)


def _digest(value: object) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(payload.encode()).hexdigest()


def _provenance(controller: str) -> StrategyProvenance:
    return StrategyProvenance(
        candidate=CompletionCandidateIdentity(
            campaign_id="campaign/one",
            target_id="target/one",
            target_generation="generation/one",
            scenario_fingerprint="scenario/one",
            technique_id="technique/one",
            controller_id=controller,
            payload_family="payload/one",
            requested_language="lzh",
            mutation_id="mutation/one",
        ),
        attempt_id="attempt/one",
        requested_controller=controller,
        actual_controller=controller,
        requested_mode="native",
        actual_mode="native",
    )


def _observation(
    *,
    payload: object | None = None,
    receipt: str = "receipt/one",
    provenance: StrategyProvenance | None = None,
    arguments: dict[str, object] | None = None,
) -> TargetObservation:
    provenance = provenance or _provenance("direct")
    return TargetObservation(
        candidate=provenance.candidate,
        attempt_id=provenance.attempt_id,
        receipt_id=receipt,
        target_session_id="session/one",
        call_id="call/one",
        arguments=arguments or {"input": "probe"},
        exchange={"response": "target reply"},
        strategy_payload=payload,
    )


def _goat_contract() -> GoatContract:
    return GoatContract(
        version="merlin.goat.v1",
        semantic_technique_id="technique/one",
        goal_digest=_digest("goal"),
        toolbox=trusted_goat_toolbox(),
        toolbox_catalog_digest=GOAT_TOOLBOX_CATALOG_DIGEST,
        terminal_oracle_digest=_digest("oracle"),
        reviewed_adapter_digest=_digest("adapter"),
        attacker_route_digest=_digest("route"),
        attacker_tool_transport="buffered",
        target_profile_id="target/one",
        session_policy="same-reviewed-session",
        modality="text",
        language="lzh",
        target_turns=2,
    )


def _crescendo_contract() -> CrescendoContract:
    return CrescendoContract(
        version="merlin.crescendo.v1",
        semantic_technique_id="technique/one",
        objective_oracle_digest=_digest("objective"),
        refusal_oracle_digest=_digest("refusal"),
        progress_oracle_digest=_digest("progress"),
        invalid_premise_oracle_digest=_digest("invalid"),
        reviewed_adapter_digest=_digest("adapter"),
        modality="text",
        language="lzh",
        max_target_turns=5,
        plateau_turns=2,
    )


def _hydra_contract() -> HydraSelectContract:
    return HydraSelectContract(
        version="merlin.hydra-select.v1",
        semantic_technique_id="technique/one",
        goal_digest=_digest("goal"),
        objective_oracle_digest=_digest("objective"),
        reviewed_adapter_digest=_digest("adapter"),
        attacker_route_digest=_digest("route"),
        attacker_tool_transport="buffered",
        target_profile_id="target/one",
        session_policy="same-reviewed-session",
        modality="text",
        language="lzh",
        mechanisms=trusted_hydra_select_mechanisms(),
        mechanism_catalog_digest=HYDRA_SELECT_CATALOG_DIGEST,
        proposed_count=6,
        selected_count=3,
        max_candidate_chars=4096,
        stop_on_positive=True,
    )


def test_direct_protocol_preserves_exact_provenance_checkpoint_and_progress() -> None:
    adapter = DirectStrategyAdapter()
    provenance = _provenance("direct")
    session = adapter.initialize(provenance)
    before = adapter.checkpoint(session)

    transition = adapter.observe_target(session, _observation(provenance=provenance))
    after = adapter.checkpoint(transition.session)

    assert transition.instruction.action == "validate"
    assert adapter.progress(transition.session, before).progressed is True
    assert adapter.completion(transition.session).candidate_retired is False
    assert adapter.restore(after, session.provenance) == transition.session


def test_duplicate_target_receipt_is_idempotent() -> None:
    adapter = DirectStrategyAdapter()
    provenance = _provenance("direct")
    session = adapter.initialize(provenance)
    observation = _observation(provenance=provenance)
    first = adapter.observe_target(session, observation).session

    second = adapter.observe_target(first, observation).session

    assert second == first
    assert len(second.target_observations) == 1


@pytest.mark.parametrize(
    "conflicting_observation",
    (
        {"receipt_id": "receipt/two"},
        {"call_id": "call/two"},
        {"exchange": {"response": "changed target reply"}},
    ),
)
def test_live_observation_rejects_receipt_or_call_identity_conflicts(
    conflicting_observation: dict[str, object],
) -> None:
    adapter = DirectStrategyAdapter()
    provenance = _provenance("direct")
    observation = _observation(provenance=provenance)
    session = adapter.observe_target(adapter.initialize(provenance), observation).session
    conflicting = TargetObservation(
        candidate=observation.candidate,
        attempt_id=observation.attempt_id,
        receipt_id=str(conflicting_observation.get("receipt_id", observation.receipt_id)),
        target_session_id=observation.target_session_id,
        call_id=str(conflicting_observation.get("call_id", observation.call_id)),
        arguments=observation.arguments,
        exchange=conflicting_observation.get("exchange", observation.exchange),
        strategy_payload=observation.strategy_payload,
    )

    with pytest.raises(ValueError, match="target (receipt|call) identity"):
        adapter.observe_target(session, conflicting)


@pytest.mark.parametrize(
    ("adapter", "controller"),
    [
        (GoatStrategyAdapter(_goat_contract()), "goat"),
        (CrescendoStrategyAdapter(_crescendo_contract()), "crescendo"),
        (HydraStrategyAdapter(_hydra_contract()), "hydra-select"),
    ],
)
def test_adaptive_wrappers_preserve_state_and_evidence_on_malformed_telemetry(
    adapter: object, controller: str
) -> None:
    session = adapter.initialize(_provenance(controller))  # type: ignore[attr-defined]
    original_state = session.state

    transition = adapter.observe_target(
        session,
        _observation(payload={"bad": True}, provenance=session.provenance),
    )  # type: ignore[attr-defined]

    assert transition.session.state.attempt_id == original_state.attempt_id
    assert transition.session.state.semantic_technique_id == original_state.semantic_technique_id
    assert transition.session.target_observations[0].receipt_id == "receipt/one"
    assert transition.instruction.action == ("compose" if controller == "crescendo" else "repair")
    assert transition.session.uncertainties[0].kind == "evaluation_uncertain"
    assert transition.session.uncertainties[0].deviation_kind == "malformed_strategy_telemetry"
    assert adapter.completion(transition.session).candidate_retired is False  # type: ignore[attr-defined]


def test_nonrepairable_deviation_preserves_state_and_requests_explicit_degradation() -> None:
    adapter = DirectStrategyAdapter()
    session = adapter.initialize(_provenance("direct"))

    transition = adapter.observe_deviation(
        session,
        StrategyDeviation(
            kind="unsupported_strategy_operation",
            source="test",
            reason="native controller is unavailable",
            repairable=False,
        ),
    )

    assert transition.session.state is session.state
    assert transition.instruction.action == "degrade"
    assert transition.session.uncertainties[0].reason == "native controller is unavailable"


def test_progress_detects_semantic_stall_without_imposing_a_step_cap() -> None:
    adapter = DirectStrategyAdapter()
    session = adapter.initialize(_provenance("direct"))
    checkpoint = adapter.checkpoint(session)

    progress = adapter.progress(session, checkpoint)

    assert progress.progressed is False
    assert progress.stalled is True
    assert adapter.next_instruction(session).action == "target"


def test_restore_rejects_provenance_drift() -> None:
    adapter = DirectStrategyAdapter()
    provenance = _provenance("direct")
    checkpoint = adapter.checkpoint(adapter.initialize(provenance))
    changed = StrategyProvenance(
        candidate=provenance.candidate,
        attempt_id="attempt/two",
        requested_controller="direct",
        actual_controller="direct",
        requested_mode="native",
        actual_mode="native",
    )

    with pytest.raises(ValueError, match="identity or integrity"):
        adapter.restore(checkpoint, changed)


@pytest.mark.parametrize("drift", ("attempt", "candidate", "duplicate"))
def test_restore_rejects_foreign_or_duplicate_target_observations(drift: str) -> None:
    adapter = DirectStrategyAdapter()
    provenance = _provenance("direct")
    session = adapter.observe_target(
        adapter.initialize(provenance), _observation(provenance=provenance)
    ).session
    checkpoint = adapter.checkpoint(session)
    observations = [dict(item) for item in checkpoint["target_observations"]]
    if drift == "attempt":
        observations[0]["attempt_id"] = "attempt/foreign"
    elif drift == "candidate":
        observations[0]["candidate"] = {
            **dict(observations[0]["candidate"]),
            "mutation_id": "mutation/foreign",
        }
    else:
        observations.append(dict(observations[0]))
    body = {
        **{key: value for key, value in checkpoint.items() if key != "checkpoint_digest"},
        "target_observations": observations,
    }
    forged = {**body, "checkpoint_digest": _digest(body)}

    with pytest.raises(ValueError, match="foreign|repeats"):
        adapter.restore(forged, provenance)


def test_strategy_contract_must_bind_candidate_technique_target_and_language() -> None:
    contract = _goat_contract()
    adapter = GoatStrategyAdapter(contract)
    provenance = _provenance("goat")

    for field, value in (
        ("technique_id", "technique/other"),
        ("target_id", "target/other"),
        ("requested_language", "sw"),
    ):
        changed = CompletionCandidateIdentity(**{**provenance.candidate.projection(), field: value})
        with pytest.raises(ValueError, match="strategy contract differs"):
            adapter.initialize(
                StrategyProvenance(
                    candidate=changed,
                    attempt_id=provenance.attempt_id,
                    requested_controller="goat",
                    actual_controller="goat",
                    requested_mode="native",
                    actual_mode="native",
                )
            )


def test_foreign_target_receipt_cannot_advance_strategy_session() -> None:
    adapter = DirectStrategyAdapter()
    provenance = _provenance("direct")
    session = adapter.initialize(provenance)
    foreign = _provenance("direct")
    foreign_candidate = CompletionCandidateIdentity(
        **{**foreign.candidate.projection(), "mutation_id": "mutation/other"}
    )
    observation = TargetObservation(
        candidate=foreign_candidate,
        attempt_id=provenance.attempt_id,
        receipt_id="receipt/foreign",
        target_session_id="session/one",
        call_id="call/foreign",
        arguments={"input": "foreign"},
        exchange={"response": "foreign"},
    )

    with pytest.raises(ValueError, match="candidate or attempt identity drifted"):
        adapter.observe_target(session, observation)


def test_degraded_strategy_executes_through_direct_plugin_with_exact_provenance() -> None:
    requested = _provenance("goat")
    provenance = StrategyProvenance(
        candidate=requested.candidate,
        attempt_id=requested.attempt_id,
        requested_controller="goat",
        actual_controller="direct",
        requested_mode="native",
        actual_mode="degraded",
    )
    adapter = DirectStrategyAdapter()
    session = adapter.initialize(provenance)

    transition = adapter.observe_target(session, _observation(provenance=provenance))

    assert transition.instruction.action == "validate"
    assert transition.session.provenance.actual_mode == "degraded"


def test_unavailable_strategy_is_explicit_retryable_state() -> None:
    requested = _provenance("goat")
    provenance = StrategyProvenance(
        candidate=requested.candidate,
        attempt_id=requested.attempt_id,
        requested_controller="goat",
        actual_controller="unavailable",
        requested_mode="native",
        actual_mode="unavailable",
    )
    adapter = UnavailableStrategyAdapter("GOAT runtime is not installed")
    session = adapter.initialize(provenance)

    assert adapter.next_instruction(session).action == "repair"
    assert adapter.completion(session).execution_complete is False
    assert adapter.restore(adapter.checkpoint(session), provenance) == session


def test_evaluator_uncertainty_is_recorded_without_forcing_strategy_repair() -> None:
    adapter = GoatStrategyAdapter(_goat_contract())
    provenance = _provenance("goat")
    session = adapter.initialize(provenance)

    payload = "continue with this target prompt"
    transition = adapter.observe_target(
        session,
        _observation(
            payload=payload,
            provenance=provenance,
            arguments={
                "input": payload,
                "strategy_plan": payload,
                "strategy_turn_index": 0,
            },
        ),
    )

    assert transition.session.uncertainties
    assert transition.instruction.action == "compose"


def test_uncertain_crescendo_receipt_does_not_fake_semantic_progress() -> None:
    adapter = CrescendoStrategyAdapter(_crescendo_contract())
    provenance = _provenance("crescendo")
    session = adapter.initialize(provenance)
    before = adapter.checkpoint(session)

    transition = adapter.observe_target(
        session,
        _observation(payload=None, provenance=provenance),
    )
    progress = adapter.progress(transition.session, before)

    assert transition.session.state.steps[0].decision == "uncertain"
    assert progress.progressed is False
    assert progress.stalled is True
