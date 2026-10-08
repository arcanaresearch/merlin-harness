"""Versioned daemon-owned Crescendo transition contract.

Concrete prompt wording remains specialist-owned. Authorized acknowledged target
effects advance the state; evaluator observations remain advisory telemetry.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from typing import Literal

TerminalReason = Literal[
    "positive",
    "plateau",
    "invalid-premise",
    "capped",
    "cancelled",
    "blocked",
    "ambiguous",
]
StepDecision = Literal[
    "success", "refusal", "progress", "no-progress", "invalid-premise", "uncertain"
]


@dataclass(frozen=True, slots=True)
class CrescendoContract:
    version: str
    semantic_technique_id: str
    objective_oracle_digest: str
    refusal_oracle_digest: str
    progress_oracle_digest: str
    invalid_premise_oracle_digest: str
    reviewed_adapter_digest: str
    modality: str
    language: str
    max_target_turns: int
    plateau_turns: int

    def __post_init__(self) -> None:
        if self.version != "merlin.crescendo.v1":
            raise ValueError("unsupported Crescendo strategy version")
        if not self.semantic_technique_id.strip():
            raise ValueError("Crescendo requires a semantic technique identity")
        for value in (
            self.objective_oracle_digest,
            self.refusal_oracle_digest,
            self.progress_oracle_digest,
            self.invalid_premise_oracle_digest,
            self.reviewed_adapter_digest,
        ):
            if not _sha256(value):
                raise ValueError("Crescendo contract digests must be full SHA-256 identities")
        oracle_digests = {
            self.objective_oracle_digest,
            self.refusal_oracle_digest,
            self.progress_oracle_digest,
            self.invalid_premise_oracle_digest,
        }
        if len(oracle_digests) != 4:
            raise ValueError("Crescendo oracle purposes require distinct digests")
        if not self.modality.strip() or not self.language.strip():
            raise ValueError("Crescendo requires explicit modality and language observations")
        if self.max_target_turns < 2:
            raise ValueError("Crescendo requires at least two reviewed target turns")
        if not 1 <= self.plateau_turns < self.max_target_turns:
            raise ValueError("Crescendo plateau bound must be below its target-turn bound")

    @property
    def digest(self) -> str:
        return _digest(asdict(self))


@dataclass(frozen=True, slots=True)
class CrescendoStep:
    index: int
    stage: Literal["probe", "escalate", "backtrack"]
    prompt_arguments_digest: str
    exchange_digest: str
    observation_digests: tuple[str, ...]
    decision: StepDecision


@dataclass(frozen=True, slots=True)
class CrescendoState:
    schema: str
    contract_digest: str
    semantic_technique_id: str
    attempt_id: str | None
    target_session_id: str | None
    next_stage: Literal["probe", "escalate", "backtrack"]
    no_progress_turns: int
    steps: tuple[CrescendoStep, ...]
    terminal_reason: TerminalReason | None = None
    evaluation_uncertainties: tuple[Mapping[str, object], ...] = ()

    @classmethod
    def start(cls, contract: CrescendoContract, attempt_id: str | None) -> CrescendoState:
        if not attempt_id:
            raise ValueError("Crescendo production state requires scheduler attempt ownership")
        return cls(
            schema="merlin.crescendo-state.v1",
            contract_digest=contract.digest,
            semantic_technique_id=contract.semantic_technique_id,
            attempt_id=attempt_id,
            target_session_id=None,
            next_stage="probe",
            no_progress_turns=0,
            steps=(),
        )

    @classmethod
    def restore(
        cls, value: object, contract: CrescendoContract, attempt_id: str | None
    ) -> CrescendoState:
        if value is None:
            return cls.start(contract, attempt_id)
        if not isinstance(value, Mapping):
            raise ValueError("Crescendo checkpoint state is malformed")
        steps_value = value.get("steps")
        if not isinstance(steps_value, Sequence) or isinstance(steps_value, (str, bytes)):
            raise ValueError("Crescendo checkpoint steps are malformed")
        state = cls(
            schema=str(value.get("schema", "")),
            contract_digest=str(value.get("contract_digest", "")),
            semantic_technique_id=str(value.get("semantic_technique_id", "")),
            attempt_id=(None if value.get("attempt_id") is None else str(value["attempt_id"])),
            target_session_id=(
                None if value.get("target_session_id") is None else str(value["target_session_id"])
            ),
            next_stage=str(value.get("next_stage", "")),  # type: ignore[arg-type]
            no_progress_turns=int(value.get("no_progress_turns", 0)),
            steps=tuple(CrescendoStep(**dict(item)) for item in steps_value),
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
            state.schema != "merlin.crescendo-state.v1"
            or state.contract_digest != contract.digest
            or state.semantic_technique_id != contract.semantic_technique_id
            or state.attempt_id != attempt_id
            or tuple(step.index for step in state.steps) != tuple(range(len(state.steps)))
        ):
            raise ValueError("Crescendo checkpoint authority or lineage drifted")
        if state.next_stage not in {"probe", "escalate", "backtrack"}:
            raise ValueError("Crescendo checkpoint stage is invalid")
        if state.no_progress_turns < 0 or len(state.steps) > contract.max_target_turns:
            raise ValueError("Crescendo checkpoint counters exceed the reviewed contract")
        allowed_terminal = {
            None,
            "positive",
            "plateau",
            "invalid-premise",
            "capped",
            "cancelled",
            "blocked",
            "ambiguous",
        }
        if state.terminal_reason not in allowed_terminal:
            raise ValueError("Crescendo checkpoint terminal reason is invalid")
        for index, step in enumerate(state.steps):
            if (
                step.index != index
                or step.stage not in {"probe", "escalate", "backtrack"}
                or step.decision
                not in {
                    "success",
                    "refusal",
                    "progress",
                    "no-progress",
                    "invalid-premise",
                    "uncertain",
                }
                or not _sha256(step.prompt_arguments_digest)
                or not _sha256(step.exchange_digest)
                or len(step.observation_digests) != 4
                or any(not _sha256(item) for item in step.observation_digests)
            ):
                raise ValueError("Crescendo checkpoint step is malformed")
        expected_stage: Literal["probe", "escalate", "backtrack"] = "probe"
        expected_no_progress = 0
        for step in state.steps:
            if step.stage != expected_stage:
                raise ValueError("Crescendo checkpoint transition history is impossible")
            expected_stage = "backtrack" if step.decision == "refusal" else "escalate"
            if step.decision == "progress":
                expected_no_progress = 0
            elif step.decision == "no-progress":
                expected_no_progress += 1
        if state.steps and (not state.target_session_id or not state.attempt_id):
            raise ValueError("Crescendo checkpoint steps lack attempt/session ownership")
        if state.next_stage != expected_stage or state.no_progress_turns != expected_no_progress:
            raise ValueError("Crescendo checkpoint derived state is incoherent")
        return state

    def advance(
        self,
        contract: CrescendoContract,
        *,
        target_session_id: str,
        prompt_arguments_digest: str,
        exchange: Mapping[str, object],
    ) -> CrescendoState:
        if self.terminal_reason is not None:
            raise ValueError("Crescendo strategy is already terminal")
        if self.contract_digest != contract.digest:
            raise ValueError("Crescendo strategy contract drifted")
        if self.target_session_id not in {None, target_session_id}:
            raise ValueError("Crescendo target session changed")
        raw_observations = exchange.get("oracle_observations")
        observations = raw_observations if isinstance(raw_observations, list) else []
        uncertainty_reasons: list[str] = []
        if not isinstance(raw_observations, list):
            uncertainty_reasons.append("oracle observation metadata was absent")
        by_purpose: dict[str, Mapping[str, object]] = {}
        for item in observations:
            if not isinstance(item, Mapping):
                uncertainty_reasons.append("an oracle observation record was malformed")
                continue
            purpose = str(item.get("purpose", ""))
            if purpose in by_purpose:
                uncertainty_reasons.append(f"oracle observation purpose {purpose!r} was duplicated")
                continue
            by_purpose[purpose] = item
        expected = {
            "objective": contract.objective_oracle_digest,
            "refusal": contract.refusal_oracle_digest,
            "progress": contract.progress_oracle_digest,
            "invalid-premise": contract.invalid_premise_oracle_digest,
        }
        trusted: dict[str, bool] = {}
        for purpose, digest in expected.items():
            observation = by_purpose.get(purpose)
            if observation is None:
                uncertainty_reasons.append(f"{purpose} observation metadata was absent")
                continue
            valid = _valid_observation(observation)
            if not valid:
                uncertainty_reasons.append(f"{purpose} observation integrity could not be verified")
            disagreements = [
                key
                for key, value in {
                    "oracle_digest": digest,
                    "adapter_digest": contract.reviewed_adapter_digest,
                    "modality": contract.modality,
                    "turn": "multi",
                    "language": contract.language,
                }.items()
                if observation.get(key) != value
            ]
            if disagreements:
                uncertainty_reasons.append(
                    f"{purpose} observation metadata disagreed on " + ", ".join(disagreements)
                )
            trusted[purpose] = valid and not disagreements and observation.get("matched") is True
        objective = trusted.get("objective", False)
        refusal = trusted.get("refusal", False)
        progress = trusted.get("progress", False)
        invalid = trusted.get("invalid-premise", False)
        if sum((objective, refusal, progress, invalid)) > 1:
            uncertainty_reasons.append("oracle observations supplied conflicting content signals")
        decision: StepDecision
        next_stage: Literal["probe", "escalate", "backtrack"] = "escalate"
        no_progress = self.no_progress_turns
        if uncertainty_reasons or sum((objective, refusal, progress, invalid)) > 1:
            decision = "uncertain"
        elif objective:
            decision = "success"
        elif invalid:
            decision = "invalid-premise"
        elif refusal:
            decision, next_stage = "refusal", "backtrack"
        elif progress:
            decision = "progress"
            no_progress = 0
        else:
            decision = "no-progress"
            no_progress += 1
        step = CrescendoStep(
            index=len(self.steps),
            stage=self.next_stage,
            prompt_arguments_digest=prompt_arguments_digest,
            exchange_digest=_digest(exchange),
            observation_digests=tuple(
                _observation_digest(by_purpose.get(purpose), purpose=purpose)
                for purpose in ("objective", "refusal", "progress", "invalid-premise")
            ),
            decision=decision,
        )
        steps = (*self.steps, step)
        return CrescendoState(
            schema=self.schema,
            contract_digest=self.contract_digest,
            semantic_technique_id=self.semantic_technique_id,
            attempt_id=self.attempt_id,
            target_session_id=target_session_id,
            next_stage=next_stage,
            no_progress_turns=no_progress,
            steps=steps,
            evaluation_uncertainties=(
                *self.evaluation_uncertainties,
                *(
                    _evaluation_uncertain(reason, source="crescendo.oracle")
                    for reason in uncertainty_reasons
                ),
            ),
        )

    def dump(self) -> dict[str, object]:
        return asdict(self)


def _valid_observation(value: Mapping[str, object]) -> bool:
    digest = value.get("observation_digest")
    unsigned = {key: item for key, item in value.items() if key != "observation_digest"}
    return (
        value.get("schema") == "merlin.api-oracle-observation.v2"
        and isinstance(digest, str)
        and digest == _digest(unsigned)
        and _sha256(str(value.get("oracle_digest", "")))
        and isinstance(value.get("matched"), bool)
        and isinstance(value.get("purpose"), str)
        and value.get("binding_digest")
        == _digest(
            {
                key: value[key]
                for key in (
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
            }
        )
    )


def _observation_digest(observation: Mapping[str, object] | None, *, purpose: str) -> str:
    if observation is not None and _sha256(str(observation.get("observation_digest", ""))):
        return str(observation["observation_digest"])
    return _digest({"purpose": purpose, "unverified_observation": observation})


def _evaluation_uncertain(reason: str, *, source: str) -> Mapping[str, object]:
    return {
        "kind": "evaluation_uncertain",
        "source": source,
        "reason": reason,
        "confidence": 0.0,
        "affects": ["ranking", "evaluation"],
    }


def _sha256(value: str) -> bool:
    if len(value) != 71 or not value.startswith("sha256:"):
        return False
    try:
        int(value[7:], 16)
    except ValueError:
        return False
    return True


def _digest(value: object) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return "sha256:" + hashlib.sha256(payload.encode()).hexdigest()
