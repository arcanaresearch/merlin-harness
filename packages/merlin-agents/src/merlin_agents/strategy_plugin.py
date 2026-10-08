"""Common evidence-first protocol for attack-strategy plugins.

The protocol deliberately separates strategy advice from candidate retirement.
Plugins may request another action, validation, repair, or an explicit budget
stop; they cannot turn evaluator telemetry into a security verdict.  Existing
strategy state machines are exposed through compatibility adapters so runtime
integration can migrate without rewriting their durable checkpoints.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, replace
from typing import Literal, Protocol, TypeVar

from .completion_frontier import CompletionCandidateIdentity
from .crescendo import CrescendoContract, CrescendoState
from .goat import GoatContract, GoatPlan, GoatState
from .hydra_select import HydraSelectContract, HydraSelectState

StrategyAction = Literal["compose", "target", "validate", "repair", "degrade", "complete"]
StrategyMode = Literal["native", "degraded", "unavailable"]
DeviationKind = Literal[
    "malformed_strategy_telemetry",
    "off_policy_action",
    "execution_deviation",
    "unsupported_strategy_operation",
]

StateT = TypeVar("StateT")
ContractT = TypeVar("ContractT")


@dataclass(frozen=True, slots=True)
class StrategyProvenance:
    """Exact requested and actual execution identity for one candidate."""

    candidate: CompletionCandidateIdentity
    attempt_id: str
    requested_controller: str
    actual_controller: str
    requested_mode: StrategyMode
    actual_mode: StrategyMode

    def __post_init__(self) -> None:
        text_fields = (
            self.attempt_id,
            self.requested_controller,
            self.actual_controller,
        )
        if any(not value.strip() for value in text_fields):
            raise ValueError("strategy provenance fields must not be empty")
        if self.candidate.controller_id != self.requested_controller:
            raise ValueError("candidate controller differs from requested controller")
        if self.requested_mode != "native":
            raise ValueError("a requested strategy mode must be native")
        if self.actual_mode == "native" and self.actual_controller != self.requested_controller:
            raise ValueError("native strategy execution cannot change controller")
        if self.actual_mode == "unavailable" and self.actual_controller != "unavailable":
            raise ValueError("unavailable execution requires explicit unavailable controller")

    def dump(self) -> dict[str, object]:
        return {
            "candidate": self.candidate.projection(),
            "attempt_id": self.attempt_id,
            "requested_controller": self.requested_controller,
            "actual_controller": self.actual_controller,
            "requested_mode": self.requested_mode,
            "actual_mode": self.actual_mode,
        }


@dataclass(frozen=True, slots=True)
class StrategyInstruction:
    action: StrategyAction
    reason: str
    stage: str | None = None
    arguments: Mapping[str, object] | None = None

    def __post_init__(self) -> None:
        if not self.reason.strip():
            raise ValueError("strategy instruction requires a reason")


@dataclass(frozen=True, slots=True)
class StrategyUncertainty:
    """Non-blocking typed observation about strategy execution quality."""

    kind: Literal["evaluation_uncertain"]
    source: str
    reason: str
    confidence: float
    deviation_kind: DeviationKind
    repairable: bool

    def __post_init__(self) -> None:
        if not self.source.strip() or not self.reason.strip():
            raise ValueError("strategy uncertainty requires source and reason")
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError("strategy uncertainty confidence must be between zero and one")

    def dump(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class StrategyDeviation:
    kind: DeviationKind
    source: str
    reason: str
    confidence: float = 0.0
    repairable: bool = True

    def uncertainty(self) -> StrategyUncertainty:
        return StrategyUncertainty(
            kind="evaluation_uncertain",
            source=self.source,
            reason=self.reason,
            confidence=self.confidence,
            deviation_kind=self.kind,
            repairable=self.repairable,
        )


@dataclass(frozen=True, slots=True)
class TargetObservation:
    """Durable evidence supplied after an authorized target effect."""

    candidate: CompletionCandidateIdentity
    attempt_id: str
    receipt_id: str
    target_session_id: str
    call_id: str
    arguments: Mapping[str, object]
    exchange: Mapping[str, object]
    tool_content: str = ""
    strategy_payload: object | None = None

    def __post_init__(self) -> None:
        if any(
            not value.strip()
            for value in (
                self.attempt_id,
                self.receipt_id,
                self.target_session_id,
                self.call_id,
            )
        ):
            raise ValueError(
                "target observation requires candidate, attempt, receipt, session, "
                "and call identity"
            )

    @property
    def evidence_digest(self) -> str:
        return _digest(self.dump())

    def dump(self) -> dict[str, object]:
        return {
            "candidate": self.candidate.projection(),
            "attempt_id": self.attempt_id,
            "receipt_id": self.receipt_id,
            "target_session_id": self.target_session_id,
            "call_id": self.call_id,
            "arguments": dict(self.arguments),
            "exchange": dict(self.exchange),
            "tool_content": self.tool_content,
            "strategy_payload": self.strategy_payload,
        }


@dataclass(frozen=True, slots=True)
class StrategySession[StateT]:
    provenance: StrategyProvenance
    state: StateT
    target_observations: tuple[TargetObservation, ...] = ()
    uncertainties: tuple[StrategyUncertainty, ...] = ()


@dataclass(frozen=True, slots=True)
class StrategyTransition[StateT]:
    session: StrategySession[StateT]
    instruction: StrategyInstruction


@dataclass(frozen=True, slots=True)
class StrategyProgress:
    state_digest: str
    observations: int
    target_receipts: int
    progressed: bool
    stalled: bool


@dataclass(frozen=True, slots=True)
class StrategyCompletion:
    execution_complete: bool
    needs_validation: bool
    reason: str
    candidate_retired: Literal[False] = False


class StrategyPlugin(Protocol[StateT]):
    strategy_id: str

    def initialize(self, provenance: StrategyProvenance) -> StrategySession[StateT]: ...

    def next_instruction(self, session: StrategySession[StateT]) -> StrategyInstruction: ...

    def observe_target(
        self, session: StrategySession[StateT], observation: TargetObservation
    ) -> StrategyTransition[StateT]: ...

    def observe_deviation(
        self, session: StrategySession[StateT], deviation: StrategyDeviation
    ) -> StrategyTransition[StateT]: ...

    def checkpoint(self, session: StrategySession[StateT]) -> dict[str, object]: ...

    def restore(
        self, checkpoint: Mapping[str, object], provenance: StrategyProvenance
    ) -> StrategySession[StateT]: ...

    def progress(
        self,
        session: StrategySession[StateT],
        previous_checkpoint: Mapping[str, object] | None = None,
    ) -> StrategyProgress: ...

    def completion(self, session: StrategySession[StateT]) -> StrategyCompletion: ...


@dataclass(frozen=True, slots=True)
class DirectStep:
    receipt_id: str
    target_session_id: str
    call_id: str
    arguments_digest: str
    exchange_digest: str


@dataclass(frozen=True, slots=True)
class DirectState:
    schema: str
    attempt_id: str
    target_session_id: str | None
    steps: tuple[DirectStep, ...]

    @classmethod
    def start(cls, attempt_id: str) -> DirectState:
        return cls("merlin.direct-state.v1", attempt_id, None, ())

    @classmethod
    def restore(cls, value: object, attempt_id: str) -> DirectState:
        if not isinstance(value, Mapping):
            raise ValueError("direct checkpoint state is malformed")
        state = cls(
            schema=str(value.get("schema", "")),
            attempt_id=str(value.get("attempt_id", "")),
            target_session_id=(
                None if value.get("target_session_id") is None else str(value["target_session_id"])
            ),
            steps=tuple(
                DirectStep(**dict(item))
                for item in _sequence(value.get("steps"))
                if isinstance(item, Mapping)
            ),
        )
        if state.schema != "merlin.direct-state.v1" or state.attempt_id != attempt_id:
            raise ValueError("direct checkpoint authority or lineage drifted")
        if state.steps and not state.target_session_id:
            raise ValueError("direct checkpoint lacks target-session ownership")
        if len({item.receipt_id for item in state.steps}) != len(state.steps):
            raise ValueError("direct checkpoint repeats a target receipt")
        return state

    def observe(self, observation: TargetObservation) -> DirectState:
        if self.target_session_id not in {None, observation.target_session_id}:
            raise ValueError("direct target session changed")
        step = DirectStep(
            receipt_id=observation.receipt_id,
            target_session_id=observation.target_session_id,
            call_id=observation.call_id,
            arguments_digest=_digest(observation.arguments),
            exchange_digest=_digest(observation.exchange),
        )
        receipt_match = next(
            (item for item in self.steps if item.receipt_id == observation.receipt_id), None
        )
        if receipt_match is not None:
            if receipt_match != step:
                raise ValueError("direct target receipt identity changed")
            return self
        if any(item.call_id == observation.call_id for item in self.steps):
            raise ValueError("direct target call identity was reused")
        return replace(
            self,
            target_session_id=observation.target_session_id,
            steps=(*self.steps, step),
        )

    def dump(self) -> dict[str, object]:
        return asdict(self)


class _LegacyStrategyAdapter[StateT, ContractT]:
    strategy_id: str

    def __init__(self, contract: ContractT) -> None:
        self.contract = contract

    def initialize(self, provenance: StrategyProvenance) -> StrategySession[StateT]:
        self._verify_provenance(provenance)
        return StrategySession(provenance, self._start(provenance.attempt_id))

    def next_instruction(self, session: StrategySession[StateT]) -> StrategyInstruction:
        self._verify_session(session)
        if len(session.target_observations) > self._observation_count(session.state) and any(
            item.deviation_kind == "malformed_strategy_telemetry" for item in session.uncertainties
        ):
            return StrategyInstruction(
                "validate",
                "target evidence exists despite malformed strategy telemetry; "
                "leave the outcome to validator judgement",
            )
        return self._next(session.state)

    def observe_target(
        self, session: StrategySession[StateT], observation: TargetObservation
    ) -> StrategyTransition[StateT]:
        self._verify_session(session)
        if (
            observation.candidate != session.provenance.candidate
            or observation.attempt_id != session.provenance.attempt_id
        ):
            raise ValueError("target observation candidate or attempt identity drifted")
        receipt_match = next(
            (
                item
                for item in session.target_observations
                if item.receipt_id == observation.receipt_id
            ),
            None,
        )
        if receipt_match is not None:
            if receipt_match != observation:
                raise ValueError("target receipt identity changed during live execution")
            return StrategyTransition(session, self._next(session.state))
        if any(item.call_id == observation.call_id for item in session.target_observations):
            raise ValueError("target call identity was reused during live execution")
        evidence_session = replace(
            session,
            target_observations=(*session.target_observations, observation),
        )
        try:
            state = self._advance(session.state, observation)
        except (KeyError, TypeError, ValueError) as exc:
            return self.observe_deviation(
                evidence_session,
                StrategyDeviation(
                    kind="malformed_strategy_telemetry",
                    source=f"strategy_plugin.{self.strategy_id}",
                    reason=str(exc) or "strategy telemetry could not be interpreted",
                    repairable=True,
                ),
            )
        before_uncertainties = _legacy_uncertainties(session.state)
        after_uncertainties = _legacy_uncertainties(state)
        new_uncertainties = tuple(
            StrategyUncertainty(
                kind="evaluation_uncertain",
                source=str(item.get("source") or f"strategy_plugin.{self.strategy_id}"),
                reason=str(item.get("reason") or "strategy telemetry could not be verified"),
                confidence=_confidence(item.get("confidence")),
                deviation_kind="malformed_strategy_telemetry",
                repairable=True,
            )
            for item in after_uncertainties
            if item not in before_uncertainties
        )
        advanced = replace(
            evidence_session,
            state=state,
            uncertainties=_dedupe_uncertainties(
                (*evidence_session.uncertainties, *new_uncertainties)
            ),
        )
        # Evaluator/oracle disagreement is evidence, not an execution fault.
        # Preserve it for validator judgement and continue the native strategy.
        return StrategyTransition(advanced, self._next(state))

    def observe_deviation(
        self, session: StrategySession[StateT], deviation: StrategyDeviation
    ) -> StrategyTransition[StateT]:
        self._verify_session(session)
        uncertainty = deviation.uncertainty()
        updated = replace(
            session,
            uncertainties=_dedupe_uncertainties((*session.uncertainties, uncertainty)),
        )
        action: Literal["repair", "degrade"] = "repair" if deviation.repairable else "degrade"
        return StrategyTransition(
            updated,
            StrategyInstruction(
                action=action,
                reason=(
                    "preserve evidence and repair the strategy observation"
                    if deviation.repairable
                    else "preserve evidence and continue through an explicitly degraded controller"
                ),
                stage="off-policy-recovery",
            ),
        )

    def checkpoint(self, session: StrategySession[StateT]) -> dict[str, object]:
        self._verify_session(session)
        body = {
            "schema": "merlin.strategy-plugin-checkpoint.v1",
            "strategy_id": self.strategy_id,
            "provenance": session.provenance.dump(),
            "state": self._dump_state(session.state),
            "target_observations": [item.dump() for item in session.target_observations],
            "uncertainties": [item.dump() for item in session.uncertainties],
        }
        return {**body, "checkpoint_digest": _digest(body)}

    def restore(
        self, checkpoint: Mapping[str, object], provenance: StrategyProvenance
    ) -> StrategySession[StateT]:
        self._verify_provenance(provenance)
        body = {key: value for key, value in checkpoint.items() if key != "checkpoint_digest"}
        if (
            checkpoint.get("schema") != "merlin.strategy-plugin-checkpoint.v1"
            or checkpoint.get("strategy_id") != self.strategy_id
            or checkpoint.get("provenance") != provenance.dump()
            or checkpoint.get("checkpoint_digest") != _digest(body)
        ):
            raise ValueError("strategy-plugin checkpoint identity or integrity drifted")
        observations = tuple(
            TargetObservation(
                **{
                    **dict(item),
                    "candidate": CompletionCandidateIdentity(**dict(item["candidate"])),
                }
            )
            for item in _sequence(checkpoint.get("target_observations"))
            if isinstance(item, Mapping) and isinstance(item.get("candidate"), Mapping)
        )
        uncertainties = tuple(
            StrategyUncertainty(**dict(item))
            for item in _sequence(checkpoint.get("uncertainties"))
            if isinstance(item, Mapping)
        )
        if len(observations) != len(_sequence(checkpoint.get("target_observations"))):
            raise ValueError("strategy-plugin checkpoint target evidence is malformed")
        if any(
            item.candidate != provenance.candidate or item.attempt_id != provenance.attempt_id
            for item in observations
        ):
            raise ValueError("strategy-plugin checkpoint contains foreign target evidence")
        if len({item.receipt_id for item in observations}) != len(observations) or len(
            {item.call_id for item in observations}
        ) != len(observations):
            raise ValueError("strategy-plugin checkpoint repeats target evidence identity")
        if len(uncertainties) != len(_sequence(checkpoint.get("uncertainties"))):
            raise ValueError("strategy-plugin checkpoint uncertainty is malformed")
        return StrategySession(
            provenance=provenance,
            state=self._restore_state(checkpoint.get("state"), provenance.attempt_id),
            target_observations=observations,
            uncertainties=_dedupe_uncertainties(uncertainties),
        )

    def progress(
        self,
        session: StrategySession[StateT],
        previous_checkpoint: Mapping[str, object] | None = None,
    ) -> StrategyProgress:
        state_digest = _digest(self._semantic_progress_state(session.state))
        previous_digest = None
        if previous_checkpoint is not None:
            previous_state = self._restore_state(
                previous_checkpoint.get("state"), session.provenance.attempt_id
            )
            previous_digest = _digest(self._semantic_progress_state(previous_state))
        progressed = previous_digest is None or state_digest != previous_digest
        complete = self.completion(session).execution_complete
        return StrategyProgress(
            state_digest=state_digest,
            observations=self._observation_count(session.state),
            target_receipts=len(session.target_observations),
            progressed=progressed,
            stalled=previous_digest is not None and not progressed and not complete,
        )

    def completion(self, session: StrategySession[StateT]) -> StrategyCompletion:
        self._verify_session(session)
        return self._completion(session.state)

    def _verify_provenance(self, provenance: StrategyProvenance) -> None:
        if provenance.actual_controller != self.strategy_id:
            raise ValueError("plugin differs from actual strategy controller")
        if provenance.actual_mode == "unavailable":
            raise ValueError("unavailable execution requires the unavailable strategy plugin")
        if provenance.actual_mode == "native" and (
            provenance.requested_controller != self.strategy_id
        ):
            raise ValueError("native compatibility plugin requires its requested controller")
        if provenance.actual_mode == "degraded" and self.strategy_id != "direct":
            raise ValueError("degraded execution must use the direct strategy plugin")
        self._verify_contract_candidate(provenance.candidate)

    def _verify_session(self, session: StrategySession[StateT]) -> None:
        self._verify_provenance(session.provenance)

    def _start(self, attempt_id: str) -> StateT:
        raise NotImplementedError

    def _restore_state(self, value: object, attempt_id: str) -> StateT:
        raise NotImplementedError

    def _verify_contract_candidate(self, candidate: CompletionCandidateIdentity) -> None:
        technique_id = getattr(self.contract, "semantic_technique_id", candidate.technique_id)
        language = getattr(self.contract, "language", candidate.requested_language)
        target_id = getattr(self.contract, "target_profile_id", candidate.target_id)
        if technique_id != candidate.technique_id:
            raise ValueError("strategy contract differs from candidate technique")
        if language != candidate.requested_language:
            raise ValueError("strategy contract differs from candidate language")
        if target_id != candidate.target_id:
            raise ValueError("strategy contract differs from candidate target")

    def _dump_state(self, state: StateT) -> dict[str, object]:
        dump = getattr(state, "dump", None)
        if not callable(dump):
            raise TypeError("strategy state does not expose a durable dump")
        value = dump()
        if not isinstance(value, dict):
            raise TypeError("strategy state dump must be a mapping")
        return value

    def _next(self, state: StateT) -> StrategyInstruction:
        raise NotImplementedError

    def _advance(self, state: StateT, observation: TargetObservation) -> StateT:
        raise NotImplementedError

    def _completion(self, state: StateT) -> StrategyCompletion:
        terminal = getattr(state, "terminal_reason", None)
        if terminal in {"cancelled", "capped"}:
            return StrategyCompletion(True, False, f"explicit operational stop: {terminal}")
        if terminal in {"awaiting-judge", "positive", "invalid-premise", "plateau"}:
            return StrategyCompletion(True, True, "accumulated evidence is ready for validation")
        return StrategyCompletion(False, False, "strategy remains unresolved")

    def _observation_count(self, state: StateT) -> int:
        steps = getattr(state, "steps", ())
        return len(steps) if isinstance(steps, Sequence) else 0

    def _semantic_progress_state(self, state: StateT) -> object:
        value = self._dump_state(state)
        value.pop("evaluation_uncertainties", None)
        return value


class DirectStrategyAdapter(_LegacyStrategyAdapter[DirectState, None]):
    strategy_id = "direct"

    def __init__(self) -> None:
        super().__init__(None)

    def _start(self, attempt_id: str) -> DirectState:
        return DirectState.start(attempt_id)

    def _restore_state(self, value: object, attempt_id: str) -> DirectState:
        return DirectState.restore(value, attempt_id)

    def _next(self, state: DirectState) -> StrategyInstruction:
        if state.steps:
            return StrategyInstruction("validate", "direct target evidence is ready for validation")
        return StrategyInstruction("target", "execute the candidate against the authorized target")

    def _advance(self, state: DirectState, observation: TargetObservation) -> DirectState:
        return state.observe(observation)

    def _completion(self, state: DirectState) -> StrategyCompletion:
        return StrategyCompletion(
            execution_complete=bool(state.steps),
            needs_validation=bool(state.steps),
            reason=("direct evidence is ready for validation" if state.steps else "not executed"),
        )


@dataclass(frozen=True, slots=True)
class UnavailableState:
    schema: str
    attempt_id: str
    reason: str

    def dump(self) -> dict[str, object]:
        return asdict(self)


class UnavailableStrategyAdapter(_LegacyStrategyAdapter[UnavailableState, str]):
    """Explicit retryable state when no truthful strategy runtime is available."""

    strategy_id = "unavailable"

    def _start(self, attempt_id: str) -> UnavailableState:
        return UnavailableState("merlin.unavailable-strategy.v1", attempt_id, self.contract)

    def _restore_state(self, value: object, attempt_id: str) -> UnavailableState:
        if not isinstance(value, Mapping):
            raise ValueError("unavailable strategy checkpoint is malformed")
        state = UnavailableState(
            schema=str(value.get("schema", "")),
            attempt_id=str(value.get("attempt_id", "")),
            reason=str(value.get("reason", "")),
        )
        if (
            state.schema != "merlin.unavailable-strategy.v1"
            or state.attempt_id != attempt_id
            or state.reason != self.contract
        ):
            raise ValueError("unavailable strategy checkpoint identity drifted")
        return state

    def _verify_provenance(self, provenance: StrategyProvenance) -> None:
        if (
            provenance.actual_mode != "unavailable"
            or provenance.actual_controller != self.strategy_id
        ):
            raise ValueError("unavailable strategy plugin requires explicit unavailable provenance")

    def _next(self, state: UnavailableState) -> StrategyInstruction:
        return StrategyInstruction(
            "repair",
            state.reason,
            "strategy-runtime-unavailable",
        )

    def _advance(self, state: UnavailableState, observation: TargetObservation) -> UnavailableState:
        raise ValueError("unavailable strategy cannot accept target observations")

    def _completion(self, state: UnavailableState) -> StrategyCompletion:
        return StrategyCompletion(False, False, state.reason)


class GoatStrategyAdapter(_LegacyStrategyAdapter[GoatState, GoatContract]):
    strategy_id = "goat"

    def _start(self, attempt_id: str) -> GoatState:
        return GoatState.start(self.contract, attempt_id)

    def _restore_state(self, value: object, attempt_id: str) -> GoatState:
        return GoatState.restore(value, self.contract, attempt_id)

    def _next(self, state: GoatState) -> StrategyInstruction:
        terminal = self._completion(state)
        if terminal.execution_complete:
            return StrategyInstruction(
                "validate" if terminal.needs_validation else "complete", terminal.reason
            )
        if len(state.steps) >= self.contract.target_turns:
            return StrategyInstruction("validate", "legacy GOAT plan is ready for validation")
        if state.pending_arguments is not None:
            return StrategyInstruction(
                "target",
                "execute the already checkpointed GOAT target action",
                stage="target-turn",
                arguments=dict(state.pending_arguments),
            )
        return StrategyInstruction("compose", "compose the next GOAT target action", "target-turn")

    def _advance(self, state: GoatState, observation: TargetObservation) -> GoatState:
        plan = GoatPlan.parse(observation.strategy_payload, self.contract)
        arguments_digest = _digest(observation.arguments)
        pending = state
        if state.pending_call_id is None:
            pending = state.with_pending(self.contract, observation.call_id, observation.arguments)
        return pending.advance(
            self.contract,
            plan=plan,
            target_session_id=observation.target_session_id,
            call_id=observation.call_id,
            tool_content=observation.tool_content,
            arguments_digest=arguments_digest,
            exchange=observation.exchange,
        )

    def _completion(self, state: GoatState) -> StrategyCompletion:
        completion = super()._completion(state)
        if completion.execution_complete:
            return completion
        if len(state.steps) >= self.contract.target_turns:
            return StrategyCompletion(True, True, "legacy GOAT plan is ready for validation")
        return completion


class CrescendoStrategyAdapter(_LegacyStrategyAdapter[CrescendoState, CrescendoContract]):
    strategy_id = "crescendo"

    def _start(self, attempt_id: str) -> CrescendoState:
        return CrescendoState.start(self.contract, attempt_id)

    def _restore_state(self, value: object, attempt_id: str) -> CrescendoState:
        return CrescendoState.restore(value, self.contract, attempt_id)

    def _next(self, state: CrescendoState) -> StrategyInstruction:
        terminal = self._completion(state)
        if terminal.execution_complete:
            return StrategyInstruction(
                "validate" if terminal.needs_validation else "complete", terminal.reason
            )
        return StrategyInstruction(
            "compose",
            "compose the next evidence-informed Crescendo action",
            stage=state.next_stage,
        )

    def _advance(self, state: CrescendoState, observation: TargetObservation) -> CrescendoState:
        return state.advance(
            self.contract,
            target_session_id=observation.target_session_id,
            prompt_arguments_digest=_digest(observation.arguments),
            exchange=observation.exchange,
        )

    def _completion(self, state: CrescendoState) -> StrategyCompletion:
        completion = super()._completion(state)
        if completion.execution_complete:
            return completion
        if state.steps and state.steps[-1].decision in {"success", "invalid-premise"}:
            return StrategyCompletion(
                True,
                True,
                "content evaluator signal is ready for validator judgement",
            )
        if len(state.steps) >= self.contract.max_target_turns:
            return StrategyCompletion(
                True,
                True,
                "accumulated Crescendo evidence is ready for validator judgement",
            )
        if state.no_progress_turns >= self.contract.plateau_turns:
            return StrategyCompletion(
                True,
                True,
                "Crescendo progress stalled; validate before allocating a mutation",
            )
        return completion

    def _semantic_progress_state(self, state: CrescendoState) -> object:
        return {
            "semantic_technique_id": state.semantic_technique_id,
            "attempt_id": state.attempt_id,
            "terminal_reason": state.terminal_reason,
            "effective_steps": [
                asdict(step)
                for step in state.steps
                if step.decision not in {"no-progress", "uncertain"}
            ],
        }


class HydraStrategyAdapter(_LegacyStrategyAdapter[HydraSelectState, HydraSelectContract]):
    strategy_id = "hydra-select"

    def _start(self, attempt_id: str) -> HydraSelectState:
        return HydraSelectState.start(self.contract, attempt_id)

    def _restore_state(self, value: object, attempt_id: str) -> HydraSelectState:
        return HydraSelectState.restore(value, self.contract, attempt_id)

    def _next(self, state: HydraSelectState) -> StrategyInstruction:
        terminal = self._completion(state)
        if terminal.execution_complete:
            return StrategyInstruction(
                "validate" if terminal.needs_validation else "complete", terminal.reason
            )
        if state.plan is None:
            return StrategyInstruction(
                "compose", "compose and persist a Hydra candidate plan", "plan"
            )
        if len(state.steps) >= len(state.plan.selected_candidate_ids):
            return StrategyInstruction(
                "validate", "selected Hydra evidence is ready for validation"
            )
        return StrategyInstruction(
            "target",
            "execute the next persisted Hydra candidate",
            stage="selected-candidate",
            arguments=state.next_arguments(),
        )

    def _advance(self, state: HydraSelectState, observation: TargetObservation) -> HydraSelectState:
        planned = state
        if state.plan is None:
            planned = state.with_plan(self.contract, observation.strategy_payload)
        pending = planned
        if planned.pending_call_id is None:
            pending = planned.with_pending(observation.call_id)
        return pending.advance(
            self.contract,
            target_session_id=observation.target_session_id,
            call_id=observation.call_id,
            arguments=observation.arguments,
            tool_content=observation.tool_content,
            exchange=observation.exchange,
        )

    def _completion(self, state: HydraSelectState) -> StrategyCompletion:
        completion = super()._completion(state)
        if completion.execution_complete:
            return completion
        if state.plan is not None and len(state.steps) >= len(state.plan.selected_candidate_ids):
            return StrategyCompletion(True, True, "selected Hydra evidence is ready for validation")
        return completion


def _sequence(value: object) -> Sequence[object]:
    return value if isinstance(value, Sequence) and not isinstance(value, (str, bytes)) else ()


def _dedupe_uncertainties(
    values: Sequence[StrategyUncertainty],
) -> tuple[StrategyUncertainty, ...]:
    return tuple(dict.fromkeys(values))


def _legacy_uncertainties(state: object) -> tuple[Mapping[str, object], ...]:
    value = getattr(state, "evaluation_uncertainties", ())
    return tuple(item for item in _sequence(value) if isinstance(item, Mapping))


def _confidence(value: object) -> float:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return min(1.0, max(0.0, float(value)))
    return 0.0


def _digest(value: object) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    return "sha256:" + hashlib.sha256(payload.encode()).hexdigest()
