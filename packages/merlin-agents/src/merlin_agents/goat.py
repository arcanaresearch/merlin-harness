"""Versioned daemon-owned GOAT conversation contract.

The attacker model may propose Observation/Thought/Strategy/Response records, but
only the daemon validates their shape and only ``Response`` is target authority.
Terminal outcome is deliberately outside this controller: a reviewed external
oracle must evaluate the completed fixed-length conversation.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from typing import Literal

from merlin_contracts import (
    GOAT_STRATEGIES,
    GOAT_TOOLBOX_CATALOG_DIGEST,
    trusted_goat_toolbox,
)

TerminalReason = Literal["awaiting-judge", "cancelled", "capped", "blocked", "ambiguous"]


@dataclass(frozen=True, slots=True)
class GoatContract:
    version: str
    semantic_technique_id: str
    goal_digest: str
    toolbox: tuple[tuple[str, str, str], ...]
    toolbox_catalog_digest: str
    terminal_oracle_digest: str
    reviewed_adapter_digest: str
    attacker_route_digest: str
    attacker_tool_transport: str
    target_profile_id: str
    session_policy: str
    modality: str
    language: str
    target_turns: int

    def __post_init__(self) -> None:
        if self.version != "merlin.goat.v1":
            raise ValueError("unsupported GOAT strategy version")
        if not self.semantic_technique_id.strip():
            raise ValueError("GOAT requires a semantic technique identity")
        if not all(
            _sha256(value)
            for value in (
                self.goal_digest,
                self.terminal_oracle_digest,
                self.reviewed_adapter_digest,
                self.attacker_route_digest,
            )
        ):
            raise ValueError("GOAT contract digests must be full SHA-256 identities")
        if tuple(name for name, _digest_value, _definition in self.toolbox) != GOAT_STRATEGIES:
            raise ValueError("GOAT requires the ordered reviewed seven-strategy toolbox")
        if self.toolbox != trusted_goat_toolbox():
            raise ValueError("GOAT toolbox differs from the daemon-owned reviewed catalogue")
        if self.toolbox_catalog_digest != GOAT_TOOLBOX_CATALOG_DIGEST:
            raise ValueError("GOAT toolbox catalogue identity drifted")
        if self.attacker_tool_transport != "buffered":
            raise ValueError("GOAT v1 requires a buffered one-call provider transport")
        if any(
            not definition.strip() or not _sha256(value) or value != _digest(definition)
            for _name, value, definition in self.toolbox
        ):
            raise ValueError("GOAT toolbox definitions require full SHA-256 identities")
        if len({value for _name, value, _definition in self.toolbox}) != len(self.toolbox):
            raise ValueError("GOAT toolbox definitions must have distinct identities")
        if not self.modality.strip() or not self.language.strip():
            raise ValueError("GOAT requires explicit modality and language observations")
        if not self.target_profile_id.strip() or self.session_policy != "same-reviewed-session":
            raise ValueError("GOAT requires exact target and shared-session authority")
        if self.target_turns < 1:
            raise ValueError("GOAT requires a positive explicit target-turn budget")

    @property
    def digest(self) -> str:
        return _digest(asdict(self))


@dataclass(frozen=True, slots=True)
class GoatPlan:
    observation: str
    thought: str
    strategies: tuple[str, ...]
    response: str
    evaluation_uncertainties: tuple[Mapping[str, object], ...] = ()

    @classmethod
    def parse(cls, value: object, contract: GoatContract) -> GoatPlan:
        uncertainties: list[Mapping[str, object]] = []
        if isinstance(value, str):
            payload: Mapping[str, object] = {"response": value}
            uncertainties.append(
                _evaluation_uncertain(
                    "attacker returned a plain response without optional O/T/S telemetry",
                    source="goat.plan",
                )
            )
        elif isinstance(value, Mapping):
            payload = value
        else:
            raise ValueError("GOAT attacker output lacks an executable response")
        response = payload.get("response")
        if not isinstance(response, str) or not response.strip() or len(response) > 16_384:
            raise ValueError("GOAT response must be a non-empty bounded string")
        expected_keys = {"observation", "thought", "strategy", "response"}
        if set(payload) != expected_keys:
            uncertainties.append(
                _evaluation_uncertain(
                    "attacker telemetry fields differed from the O/T/S/R shape",
                    source="goat.plan",
                )
            )
        telemetry: dict[str, str] = {}
        for name in ("observation", "thought"):
            item = payload.get(name)
            if isinstance(item, str) and len(item) <= 16_384:
                telemetry[name] = item
            else:
                telemetry[name] = ""
                uncertainties.append(
                    _evaluation_uncertain(
                        f"attacker {name} telemetry was absent or malformed",
                        source="goat.plan",
                    )
                )
        raw_strategies = payload.get("strategy", ())
        if isinstance(raw_strategies, str):
            supplied = (raw_strategies,)
            uncertainties.append(
                _evaluation_uncertain(
                    "attacker strategy telemetry was not an ordered list",
                    source="goat.plan",
                )
            )
        elif isinstance(raw_strategies, Sequence) and not isinstance(raw_strategies, (str, bytes)):
            supplied = tuple(str(item) for item in raw_strategies if str(item).strip())
        else:
            supplied = ()
            uncertainties.append(
                _evaluation_uncertain(
                    "attacker strategy telemetry was absent or malformed",
                    source="goat.plan",
                )
            )
        normalized = tuple(dict.fromkeys(item for item in supplied if item in GOAT_STRATEGIES))
        rejected = tuple(item for item in supplied if item not in GOAT_STRATEGIES)
        if rejected or len(normalized) != len(supplied):
            uncertainties.append(
                _evaluation_uncertain(
                    "attacker strategy telemetry contained unknown or duplicate labels",
                    source="goat.plan",
                )
            )
        return cls(
            observation=telemetry["observation"],
            thought=telemetry["thought"],
            strategies=normalized,
            response=response,
            evaluation_uncertainties=tuple(uncertainties),
        )

    @property
    def telemetry_digest(self) -> str:
        return _digest(
            {
                "observation": self.observation,
                "thought": self.thought,
                "strategies": self.strategies,
            }
        )

    @property
    def response_digest(self) -> str:
        return _digest(self.response)


@dataclass(frozen=True, slots=True)
class GoatStep:
    index: int
    call_id: str
    telemetry_digest: str
    strategy_ids: tuple[str, ...]
    response_digest: str
    arguments_digest: str
    exchange_digest: str
    objective_observation_digest: str
    objective_matched: bool | None
    plan: dict[str, object]
    target_reply: object
    target_reply_digest: str
    tool_content: str


@dataclass(frozen=True, slots=True)
class GoatState:
    schema: str
    contract_digest: str
    semantic_technique_id: str
    attempt_id: str
    target_session_id: str | None
    steps: tuple[GoatStep, ...]
    pending_call_id: str | None = None
    pending_arguments: dict[str, object] | None = None
    pending_arguments_digest: str | None = None
    terminal_reason: TerminalReason | None = None
    evaluation_uncertainties: tuple[Mapping[str, object], ...] = ()

    @classmethod
    def start(cls, contract: GoatContract, attempt_id: str | None) -> GoatState:
        if not attempt_id:
            raise ValueError("GOAT production state requires scheduler attempt ownership")
        return cls(
            schema="merlin.goat-state.v1",
            contract_digest=contract.digest,
            semantic_technique_id=contract.semantic_technique_id,
            attempt_id=attempt_id,
            target_session_id=None,
            steps=(),
        )

    @classmethod
    def restore(cls, value: object, contract: GoatContract, attempt_id: str | None) -> GoatState:
        if value is None:
            return cls.start(contract, attempt_id)
        if not isinstance(value, Mapping):
            raise ValueError("GOAT checkpoint state is malformed")
        raw_steps = value.get("steps")
        if not isinstance(raw_steps, Sequence) or isinstance(raw_steps, (str, bytes)):
            raise ValueError("GOAT checkpoint steps are malformed")
        state = cls(
            schema=str(value.get("schema", "")),
            contract_digest=str(value.get("contract_digest", "")),
            semantic_technique_id=str(value.get("semantic_technique_id", "")),
            attempt_id=str(value.get("attempt_id", "")),
            target_session_id=(
                None if value.get("target_session_id") is None else str(value["target_session_id"])
            ),
            steps=tuple(GoatStep(**dict(item)) for item in raw_steps),
            pending_call_id=(
                None if value.get("pending_call_id") is None else str(value["pending_call_id"])
            ),
            pending_arguments=(
                None if value.get("pending_arguments") is None else dict(value["pending_arguments"])
            ),
            pending_arguments_digest=(
                None
                if value.get("pending_arguments_digest") is None
                else str(value["pending_arguments_digest"])
            ),
            terminal_reason=(
                None if value.get("terminal_reason") is None else str(value["terminal_reason"])
            ),  # type: ignore[arg-type]
            evaluation_uncertainties=tuple(
                dict(item)
                for item in value.get("evaluation_uncertainties", ())
                if isinstance(item, Mapping)
            ),
        )
        if (
            state.schema != "merlin.goat-state.v1"
            or state.contract_digest != contract.digest
            or state.semantic_technique_id != contract.semantic_technique_id
            or state.attempt_id != attempt_id
            or tuple(step.index for step in state.steps) != tuple(range(len(state.steps)))
            or len(state.steps) > contract.target_turns
        ):
            raise ValueError("GOAT checkpoint authority or lineage drifted")
        for step in state.steps:
            restored_plan = GoatPlan.parse(step.plan, contract)
            if (
                any(item not in GOAT_STRATEGIES for item in step.strategy_ids)
                or len(set(step.strategy_ids)) != len(step.strategy_ids)
                or any(
                    not _sha256(item)
                    for item in (
                        step.telemetry_digest,
                        step.response_digest,
                        step.arguments_digest,
                        step.exchange_digest,
                        step.objective_observation_digest,
                    )
                )
                or restored_plan.telemetry_digest != step.telemetry_digest
                or restored_plan.response_digest != step.response_digest
                or step.target_reply_digest != _digest(step.target_reply)
                or not isinstance(step.tool_content, str)
            ):
                raise ValueError("GOAT checkpoint step is malformed")
        planned_observations_complete = len(state.steps) == contract.target_turns
        allowed_terminal = {None, "cancelled", "capped", "blocked", "ambiguous"}
        if planned_observations_complete:
            allowed_terminal.add("awaiting-judge")
        if state.terminal_reason not in allowed_terminal:
            raise ValueError("GOAT checkpoint terminal state is incoherent")
        if state.steps and not state.target_session_id:
            raise ValueError("GOAT checkpoint steps lack target-session ownership")
        pending_values = (
            state.pending_call_id,
            state.pending_arguments,
            state.pending_arguments_digest,
        )
        if any(item is not None for item in pending_values):
            if not all(item is not None for item in pending_values):
                raise ValueError("GOAT pending target plan is incomplete")
            if state.terminal_reason is not None:
                raise ValueError("GOAT terminal state cannot retain a pending target plan")
            plan = GoatPlan.parse(state.pending_arguments.get("strategy_plan"), contract)  # type: ignore[union-attr]
            if (
                not state.pending_call_id.strip()  # type: ignore[union-attr]
                or set(state.pending_arguments)
                != {  # type: ignore[arg-type]
                    "input",
                    "strategy_plan",
                    "strategy_turn_index",
                }
                or state.pending_arguments.get("input") != plan.response  # type: ignore[union-attr]
                or state.pending_arguments.get("strategy_turn_index") != len(state.steps)  # type: ignore[union-attr]
                or state.pending_arguments_digest != _digest(state.pending_arguments)
            ):
                raise ValueError("GOAT pending target plan drifted")
        return state

    def with_pending(
        self, contract: GoatContract, call_id: str, arguments: Mapping[str, object]
    ) -> GoatState:
        if self.pending_call_id is not None or self.terminal_reason is not None:
            raise ValueError("GOAT already has pending or terminal target state")
        plan = GoatPlan.parse(arguments.get("strategy_plan"), contract)
        if (
            not call_id.strip()
            or set(arguments) != {"input", "strategy_plan", "strategy_turn_index"}
            or arguments.get("input") != plan.response
            or arguments.get("strategy_turn_index") != len(self.steps)
        ):
            raise ValueError("GOAT pending target plan is not exact")
        return GoatState(
            schema=self.schema,
            contract_digest=self.contract_digest,
            semantic_technique_id=self.semantic_technique_id,
            attempt_id=self.attempt_id,
            target_session_id=self.target_session_id,
            steps=self.steps,
            pending_call_id=call_id,
            pending_arguments=dict(arguments),
            pending_arguments_digest=_digest(arguments),
            evaluation_uncertainties=self.evaluation_uncertainties,
        )

    def advance(
        self,
        contract: GoatContract,
        *,
        plan: GoatPlan,
        target_session_id: str,
        call_id: str,
        tool_content: str,
        arguments_digest: str,
        exchange: Mapping[str, object],
    ) -> GoatState:
        if self.terminal_reason is not None or len(self.steps) >= contract.target_turns:
            raise ValueError("GOAT strategy is already terminal")
        if self.contract_digest != contract.digest:
            raise ValueError("GOAT strategy contract drifted")
        if self.target_session_id not in {None, target_session_id}:
            raise ValueError("GOAT target session changed")
        if not call_id.strip() or not _sha256(arguments_digest):
            raise ValueError("GOAT target arguments lack an exact digest")
        if self.pending_call_id is not None and (
            self.pending_call_id != call_id or self.pending_arguments_digest != arguments_digest
        ):
            raise ValueError("GOAT acknowledged target effect drifted from pending plan")
        observation, observation_matched, uncertainties = _objective_observation(
            exchange,
            oracle_digest=contract.terminal_oracle_digest,
            adapter_digest=contract.reviewed_adapter_digest,
            modality=contract.modality,
            language=contract.language,
        )
        step = GoatStep(
            index=len(self.steps),
            call_id=call_id,
            telemetry_digest=plan.telemetry_digest,
            strategy_ids=plan.strategies,
            response_digest=plan.response_digest,
            arguments_digest=arguments_digest,
            exchange_digest=_digest(exchange),
            objective_observation_digest=_observation_digest(observation),
            objective_matched=observation_matched,
            plan={
                "observation": plan.observation,
                "thought": plan.thought,
                "strategy": list(plan.strategies),
                "response": plan.response,
            },
            target_reply=exchange.get("response"),
            target_reply_digest=_digest(exchange.get("response")),
            tool_content=tool_content,
        )
        steps = (*self.steps, step)
        return GoatState(
            schema=self.schema,
            contract_digest=self.contract_digest,
            semantic_technique_id=self.semantic_technique_id,
            attempt_id=self.attempt_id,
            target_session_id=target_session_id,
            steps=steps,
            evaluation_uncertainties=(
                *self.evaluation_uncertainties,
                *plan.evaluation_uncertainties,
                *uncertainties,
            ),
        )

    def dump(self) -> dict[str, object]:
        return asdict(self)


def _sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and value.startswith("sha256:")
        and len(value) == 71
        and all(character in "0123456789abcdef" for character in value[7:])
    )


def _digest(value: object) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _valid_observation(value: Mapping[str, object]) -> bool:
    digest = value.get("observation_digest")
    body = {key: item for key, item in value.items() if key != "observation_digest"}
    binding_keys = (
        "adapter_digest",
        "step_id",
        "oracle_digest",
        "pointer",
        "match_values",
        "modality",
        "turn",
        "language",
        "purpose",
    )
    if "compiled_adapter_digest" in value:
        binding_keys = (*binding_keys, "compiled_adapter_digest")
    return (
        value.get("schema") == "merlin.api-oracle-observation.v2"
        and type(value.get("matched")) is bool
        and _sha256(value.get("oracle_digest"))
        and _sha256(value.get("adapter_digest"))
        and (
            "compiled_adapter_digest" not in value or _sha256(value.get("compiled_adapter_digest"))
        )
        and digest == _digest(body)
        and all(key in value for key in binding_keys)
        and value.get("binding_digest") == _digest({key: value[key] for key in binding_keys})
    )


def _objective_observation(
    exchange: Mapping[str, object],
    *,
    oracle_digest: str,
    adapter_digest: str,
    modality: str,
    language: str,
) -> tuple[Mapping[str, object] | None, bool | None, tuple[Mapping[str, object], ...]]:
    raw = exchange.get("oracle_observations")
    observations = (
        [item for item in raw if isinstance(item, Mapping)] if isinstance(raw, list) else []
    )
    objectives = [item for item in observations if item.get("purpose") == "objective"]
    reasons: list[str] = []
    if not isinstance(raw, list):
        reasons.append("objective observation metadata was absent")
    elif len(observations) != len(raw):
        reasons.append("an objective observation record was malformed")
    purposes = [str(item.get("purpose", "")) for item in observations]
    if len(set(purposes)) != len(purposes):
        reasons.append("objective observation purposes were duplicated")
    if len(objectives) != 1:
        reasons.append("exactly one objective observation was not available")
    observation = objectives[0] if objectives else None
    expected = {
        "purpose": "objective",
        "oracle_digest": oracle_digest,
        "adapter_digest": adapter_digest,
        "modality": modality,
        "turn": "multi",
        "language": language,
    }
    if observation is not None:
        if not _valid_observation(observation):
            reasons.append("objective observation integrity could not be verified")
        disagreements = [key for key, value in expected.items() if observation.get(key) != value]
        if disagreements:
            reasons.append(
                "objective observation metadata disagreed on " + ", ".join(disagreements)
            )
    trusted = observation is not None and not reasons and type(observation.get("matched")) is bool
    return (
        observation,
        observation.get("matched") is True if trusted else None,
        tuple(_evaluation_uncertain(reason, source="goat.oracle") for reason in reasons),
    )


def _observation_digest(observation: Mapping[str, object] | None) -> str:
    if observation is not None and _sha256(observation.get("observation_digest")):
        return str(observation["observation_digest"])
    return _digest({"unverified_observation": observation})


def _evaluation_uncertain(reason: str, *, source: str) -> Mapping[str, object]:
    return {
        "kind": "evaluation_uncertain",
        "source": source,
        "reason": reason,
        "confidence": 0.0,
        "affects": ["ranking", "evaluation"],
    }
