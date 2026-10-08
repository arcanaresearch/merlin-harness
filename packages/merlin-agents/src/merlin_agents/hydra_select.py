"""Merlin-native bounded Hydra proposal and deterministic selection contract.

Hydra candidates are low-trust attacker output.  The daemon derives every
candidate identity, performs exact canonical deduplication, and selects a
bounded ordered subset using only sealed deterministic features.  Candidate
generation never creates coverage cells; only acknowledged selected target
effects are execution evidence.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, replace
from typing import Literal

from merlin_contracts import HYDRA_SELECT_CATALOG_DIGEST, trusted_hydra_select_mechanisms

TerminalReason = Literal["awaiting-judge", "cancelled", "capped", "blocked", "ambiguous"]


@dataclass(frozen=True, slots=True)
class HydraSelectContract:
    version: str
    semantic_technique_id: str
    goal_digest: str
    objective_oracle_digest: str
    reviewed_adapter_digest: str
    attacker_route_digest: str
    attacker_tool_transport: str
    target_profile_id: str
    session_policy: str
    modality: str
    language: str
    mechanisms: tuple[tuple[str, str, str], ...]
    mechanism_catalog_digest: str
    proposed_count: int
    selected_count: int
    max_candidate_chars: int
    stop_on_positive: bool

    def __post_init__(self) -> None:
        if self.version != "merlin.hydra-select.v1":
            raise ValueError("unsupported Hydra-Select strategy version")
        if not self.semantic_technique_id.strip():
            raise ValueError("Hydra-Select requires a semantic technique identity")
        if not all(
            _sha256(value)
            for value in (
                self.goal_digest,
                self.objective_oracle_digest,
                self.reviewed_adapter_digest,
                self.attacker_route_digest,
            )
        ):
            raise ValueError("Hydra-Select contract digests must be full SHA-256 identities")
        if self.attacker_tool_transport != "buffered":
            raise ValueError("Hydra-Select v1 requires buffered tool calls")
        if not self.modality.strip() or not self.language.strip():
            raise ValueError("Hydra-Select requires explicit modality and language observations")
        if not self.target_profile_id.strip() or self.session_policy != "same-reviewed-session":
            raise ValueError("Hydra-Select requires exact target and shared-session authority")
        if self.mechanisms != trusted_hydra_select_mechanisms():
            raise ValueError("Hydra-Select mechanisms differ from the daemon-owned catalogue")
        if self.mechanism_catalog_digest != HYDRA_SELECT_CATALOG_DIGEST:
            raise ValueError("Hydra-Select mechanism catalogue identity drifted")
        if not 1 <= self.proposed_count <= len(self.mechanisms):
            raise ValueError("Hydra-Select proposal budget exceeds available mechanisms")
        if not 1 <= self.selected_count <= min(8, self.proposed_count):
            raise ValueError("Hydra-Select selected count must be between 1 and 8")
        if self.selected_count > len(self.mechanisms):
            raise ValueError("Hydra-Select selection width exceeds reviewed mechanism diversity")
        if not 32 <= self.max_candidate_chars <= 16_384:
            raise ValueError("Hydra-Select candidate bound is invalid")
        if type(self.stop_on_positive) is not bool:
            raise ValueError("Hydra-Select stopping policy must be explicit")

    @property
    def digest(self) -> str:
        return _digest(asdict(self))


@dataclass(frozen=True, slots=True)
class HydraCandidate:
    candidate_id: str
    prompt: str
    requested_mechanism_id: str
    mechanism_digest: str
    rationale: str
    prompt_digest: str
    canonical_digest: str
    rank: int
    rank_reason: str
    source_index: int
    disposition: str
    duplicate_of: str | None


@dataclass(frozen=True, slots=True)
class HydraPlan:
    schema: str
    contract_digest: str
    candidates: tuple[HydraCandidate, ...]
    selected_candidate_ids: tuple[str, ...]
    plan_digest: str
    evaluation_uncertainties: tuple[Mapping[str, object], ...] = ()

    @classmethod
    def parse(cls, value: object, contract: HydraSelectContract) -> HydraPlan:
        if not isinstance(value, Mapping) or "candidates" not in value:
            raise ValueError("Hydra-Select output lacks an executable candidate list")
        uncertainties: list[Mapping[str, object]] = []
        if set(value) != {"candidates"}:
            uncertainties.append(
                _evaluation_uncertain(
                    "candidate-plan telemetry contained unexpected fields", source="hydra.plan"
                )
            )
        raw = value["candidates"]
        if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
            raise ValueError("Hydra-Select candidates must be an ordered list")
        if len(raw) != contract.proposed_count:
            uncertainties.append(
                _evaluation_uncertain(
                    "candidate count differed from the proposal budget", source="hydra.plan"
                )
            )
        usable: list[tuple[int, str, str]] = []
        for index, item in enumerate(raw[: contract.proposed_count]):
            if isinstance(item, str):
                prompt = item
                rationale = ""
                uncertainties.append(
                    _evaluation_uncertain(
                        f"candidate {index} omitted optional rationale telemetry",
                        source="hydra.plan",
                    )
                )
            elif isinstance(item, Mapping):
                prompt = item.get("prompt")
                rationale_value = item.get("rationale")
                rationale = (
                    rationale_value
                    if isinstance(rationale_value, str) and len(rationale_value) <= 4_096
                    else ""
                )
                if set(item) != {"prompt", "rationale"} or not rationale.strip():
                    uncertainties.append(
                        _evaluation_uncertain(
                            f"candidate {index} telemetry fields were incomplete or unexpected",
                            source="hydra.plan",
                        )
                    )
            else:
                uncertainties.append(
                    _evaluation_uncertain(
                        f"candidate {index} was malformed and could not execute",
                        source="hydra.plan",
                    )
                )
                continue
            if (
                not isinstance(prompt, str)
                or not prompt.strip()
                or len(prompt) > contract.max_candidate_chars
            ):
                uncertainties.append(
                    _evaluation_uncertain(
                        f"candidate {index} prompt was empty or exceeded its explicit budget",
                        source="hydra.plan",
                    )
                )
                continue
            usable.append((index, prompt, rationale))
        if len(raw) > contract.proposed_count:
            uncertainties.append(
                _evaluation_uncertain(
                    "candidates beyond the explicit proposal budget were not scheduled",
                    source="hydra.plan",
                )
            )
        if not usable:
            raise ValueError("Hydra-Select output contained no executable candidates")
        rows: list[dict[str, object]] = []
        first_by_canonical: dict[str, str] = {}
        for index, prompt, rationale in usable:
            normalized = _canonical_prompt(prompt)
            prompt_digest = _digest(prompt)
            canonical_digest = _digest(normalized)
            mechanism_id, mechanism_digest, _definition = contract.mechanisms[index]
            candidate_id = (
                "candidate-"
                + _digest({"source_index": index, "canonical_digest": canonical_digest})[7:23]
            )
            duplicate_of = first_by_canonical.get(canonical_digest)
            if duplicate_of is None:
                first_by_canonical[canonical_digest] = candidate_id
            rows.append(
                {
                    "candidate_id": candidate_id,
                    "prompt": prompt,
                    "requested_mechanism_id": mechanism_id,
                    "mechanism_digest": mechanism_digest,
                    "rationale": rationale,
                    "prompt_digest": prompt_digest,
                    "canonical_digest": canonical_digest,
                    "source_index": index,
                    "duplicate_of": duplicate_of,
                }
            )
        if len(first_by_canonical) < contract.selected_count:
            uncertainties.append(
                _evaluation_uncertain(
                    "candidate plan supplied less executable diversity than requested",
                    source="hydra.plan",
                )
            )
        ordered = sorted(
            (row for row in rows if row["duplicate_of"] is None),
            key=lambda row: (
                _digest(
                    {
                        "selector": "merlin.hydra-select.catalogue-sample.v1",
                        "goal_digest": contract.goal_digest,
                        "mechanism_digest": row["mechanism_digest"],
                    }
                ),
                int(row["source_index"]),
            ),
        )
        rank_by_id = {str(row["candidate_id"]): rank for rank, row in enumerate(ordered)}
        for row in rows:
            duplicate_of = row["duplicate_of"]
            row["rank"] = (
                rank_by_id[str(duplicate_of)]
                if duplicate_of is not None
                else rank_by_id[str(row["candidate_id"])]
            )
            row["rank_reason"] = (
                "exact canonical duplicate; zero target authority"
                if duplicate_of is not None
                else "daemon catalogue diversity sample; attacker rationale ignored"
            )
        selected = tuple(str(row["candidate_id"]) for row in ordered[: contract.selected_count])
        candidates = tuple(
            HydraCandidate(
                **row,
                disposition=(
                    "duplicate"
                    if row["duplicate_of"] is not None
                    else "selected"
                    if row["candidate_id"] in selected
                    else "not-selected"
                ),
            )
            for row in rows
        )
        body = {
            "schema": "merlin.hydra-select-plan.v1",
            "contract_digest": contract.digest,
            "candidates": [asdict(item) for item in candidates],
            "selected_candidate_ids": selected,
        }
        return cls(
            schema="merlin.hydra-select-plan.v1",
            contract_digest=contract.digest,
            candidates=candidates,
            selected_candidate_ids=selected,
            plan_digest=_digest(body),
            evaluation_uncertainties=tuple(uncertainties),
        )

    def selected(self) -> tuple[HydraCandidate, ...]:
        by_id = {item.candidate_id: item for item in self.candidates}
        return tuple(by_id[item] for item in self.selected_candidate_ids)


@dataclass(frozen=True, slots=True)
class HydraStep:
    index: int
    scheduler_attempt_id: str
    candidate_id: str
    call_id: str
    arguments_digest: str
    exchange_digest: str
    exchange_id: str
    objective_observation_digest: str
    objective_matched: bool | None
    target_reply_digest: str
    tool_content: str
    # Filled by durable recovery after the broker-owned target effect exists.
    # A live runtime checkpoint may not yet know the repository identity.
    effect_id: str | None = None


@dataclass(frozen=True, slots=True)
class HydraSelectState:
    schema: str
    contract_digest: str
    semantic_technique_id: str
    attempt_id: str
    plan: HydraPlan | None
    target_session_id: str | None
    steps: tuple[HydraStep, ...]
    pending_call_id: str | None = None
    pending_arguments: dict[str, object] | None = None
    pending_arguments_digest: str | None = None
    terminal_reason: TerminalReason | None = None
    blocking_effect_id: str | None = None
    blocking_effect_attempt_id: str | None = None
    evaluation_uncertainties: tuple[Mapping[str, object], ...] = ()

    @classmethod
    def start(cls, contract: HydraSelectContract, attempt_id: str | None) -> HydraSelectState:
        if not attempt_id:
            raise ValueError("Hydra-Select production state requires scheduler attempt ownership")
        return cls(
            schema="merlin.hydra-select-state.v1",
            contract_digest=contract.digest,
            semantic_technique_id=contract.semantic_technique_id,
            attempt_id=attempt_id,
            plan=None,
            target_session_id=None,
            steps=(),
        )

    @classmethod
    def restore(
        cls, value: object, contract: HydraSelectContract, attempt_id: str | None
    ) -> HydraSelectState:
        if value is None:
            return cls.start(contract, attempt_id)
        if not isinstance(value, Mapping):
            raise ValueError("Hydra-Select checkpoint state is malformed")
        plan_value = value.get("plan")
        plan = None
        if plan_value is not None:
            if not isinstance(plan_value, Mapping):
                raise ValueError("Hydra-Select checkpoint plan is malformed")
            candidates = tuple(HydraCandidate(**dict(item)) for item in plan_value["candidates"])
            plan = HydraPlan(
                schema=str(plan_value["schema"]),
                contract_digest=str(plan_value["contract_digest"]),
                candidates=candidates,
                selected_candidate_ids=tuple(plan_value["selected_candidate_ids"]),
                plan_digest=str(plan_value["plan_digest"]),
                evaluation_uncertainties=tuple(
                    dict(item)
                    for item in plan_value.get("evaluation_uncertainties", ())
                    if isinstance(item, Mapping)
                ),
            )
        state = cls(
            schema=str(value.get("schema", "")),
            contract_digest=str(value.get("contract_digest", "")),
            semantic_technique_id=str(value.get("semantic_technique_id", "")),
            attempt_id=str(value.get("attempt_id", "")),
            plan=plan,
            target_session_id=(
                None if value.get("target_session_id") is None else str(value["target_session_id"])
            ),
            steps=tuple(HydraStep(**dict(item)) for item in value.get("steps", ())),
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
            blocking_effect_id=(
                None
                if value.get("blocking_effect_id") is None
                else str(value["blocking_effect_id"])
            ),
            blocking_effect_attempt_id=(
                None
                if value.get("blocking_effect_attempt_id") is None
                else str(value["blocking_effect_attempt_id"])
            ),
            evaluation_uncertainties=tuple(
                dict(item)
                for item in value.get("evaluation_uncertainties", ())
                if isinstance(item, Mapping)
            ),
        )
        state._validate(contract, attempt_id)
        return state

    def _validate(self, contract: HydraSelectContract, attempt_id: str | None) -> None:
        if (
            self.schema != "merlin.hydra-select-state.v1"
            or self.contract_digest != contract.digest
            or self.semantic_technique_id != contract.semantic_technique_id
            or self.attempt_id != attempt_id
            or tuple(step.index for step in self.steps) != tuple(range(len(self.steps)))
        ):
            raise ValueError("Hydra-Select checkpoint authority or lineage drifted")
        if self.plan is None and (self.steps or self.pending_call_id is not None):
            raise ValueError("Hydra-Select target state lacks its persisted selection plan")
        if self.plan is not None:
            body = {
                "schema": self.plan.schema,
                "contract_digest": self.plan.contract_digest,
                "candidates": [asdict(item) for item in self.plan.candidates],
                "selected_candidate_ids": self.plan.selected_candidate_ids,
                "evaluation_uncertainties": list(self.plan.evaluation_uncertainties),
            }
            legacy_body = {
                key: item for key, item in body.items() if key != "evaluation_uncertainties"
            }
            # The executable plan core remains sealed by the legacy digest while
            # advisory evaluation uncertainty is persisted alongside it.  Accept
            # both encodings so adding observations cannot invalidate an already
            # acknowledged candidate plan during durable restore.
            plan_digest_valid = self.plan.plan_digest in {
                _digest(body),
                _digest(legacy_body),
            }
            candidate_ids = {item.candidate_id for item in self.plan.candidates}
            source_indexes = {item.source_index for item in self.plan.candidates}
            candidate_integrity = all(
                0 <= item.source_index < contract.proposed_count
                and item.requested_mechanism_id == contract.mechanisms[item.source_index][0]
                and item.mechanism_digest == contract.mechanisms[item.source_index][1]
                and item.prompt.strip()
                and len(item.prompt) <= contract.max_candidate_chars
                and item.prompt_digest == _digest(item.prompt)
                and item.canonical_digest == _digest(_canonical_prompt(item.prompt))
                and item.candidate_id
                == "candidate-"
                + _digest(
                    {
                        "source_index": item.source_index,
                        "canonical_digest": item.canonical_digest,
                    }
                )[7:23]
                and item.disposition in {"selected", "not-selected", "duplicate"}
                for item in self.plan.candidates
            )
            if (
                self.plan.schema != "merlin.hydra-select-plan.v1"
                or self.plan.contract_digest != contract.digest
                or not plan_digest_valid
                or not candidate_integrity
                or len(candidate_ids) != len(self.plan.candidates)
                or len(source_indexes) != len(self.plan.candidates)
                or len(self.plan.selected_candidate_ids) > contract.selected_count
                or any(item not in candidate_ids for item in self.plan.selected_candidate_ids)
                or any(
                    (item.disposition == "selected")
                    != (item.candidate_id in self.plan.selected_candidate_ids)
                    for item in self.plan.candidates
                    if item.duplicate_of is None
                )
                or tuple(step.candidate_id for step in self.steps)
                != self.plan.selected_candidate_ids[: len(self.steps)]
                or len(self.steps) > len(self.plan.selected_candidate_ids)
            ):
                raise ValueError("Hydra-Select persisted selection plan drifted")
        if self.steps and not self.target_session_id:
            raise ValueError("Hydra-Select steps lack target-session ownership")
        positive = any(step.objective_matched for step in self.steps)
        observations_ready_for_review = self.plan is not None and (
            len(self.steps) == len(self.plan.selected_candidate_ids)
            or contract.stop_on_positive
            and positive
        )
        allowed_terminal = {None, "cancelled", "capped", "blocked", "ambiguous"}
        if observations_ready_for_review:
            allowed_terminal.add("awaiting-judge")
        if self.terminal_reason not in allowed_terminal:
            raise ValueError("Hydra-Select terminal state is incoherent")
        if (
            (self.blocking_effect_id is None) != (self.blocking_effect_attempt_id is None)
            or self.blocking_effect_id is not None
            and self.terminal_reason != "ambiguous"
        ):
            raise ValueError("Hydra-Select blocking effect lineage is incoherent")
        pending = (self.pending_call_id, self.pending_arguments, self.pending_arguments_digest)
        if any(item is not None for item in pending):
            if not all(item is not None for item in pending) or self.terminal_reason is not None:
                raise ValueError("Hydra-Select pending target plan is incomplete")
            expected_arguments = self.next_arguments()
            if (
                self.pending_arguments != expected_arguments
                or self.pending_arguments_digest != _digest(expected_arguments)
            ):
                raise ValueError("Hydra-Select pending target plan drifted")

    def with_plan(self, contract: HydraSelectContract, value: object) -> HydraSelectState:
        if self.plan is not None or self.steps or self.pending_call_id is not None:
            raise ValueError("Hydra-Select proposal is already persisted")
        plan = HydraPlan.parse(value, contract)
        return replace(
            self,
            plan=plan,
            evaluation_uncertainties=(
                *self.evaluation_uncertainties,
                *plan.evaluation_uncertainties,
            ),
        )

    def next_arguments(self) -> dict[str, object]:
        if self.plan is None or len(self.steps) >= len(self.plan.selected_candidate_ids):
            raise ValueError("Hydra-Select has no remaining selected candidate")
        candidate = self.plan.selected()[len(self.steps)]
        return {
            "input": candidate.prompt,
        }

    def with_pending(self, call_id: str) -> HydraSelectState:
        arguments = self.next_arguments()
        if (
            not call_id.strip()
            or self.pending_call_id is not None
            or self.terminal_reason is not None
        ):
            raise ValueError("Hydra-Select cannot persist this pending target call")
        return replace(
            self,
            pending_call_id=call_id,
            pending_arguments=arguments,
            pending_arguments_digest=_digest(arguments),
        )

    def advance(
        self,
        contract: HydraSelectContract,
        *,
        target_session_id: str,
        call_id: str,
        arguments: Mapping[str, object],
        tool_content: str,
        exchange: Mapping[str, object],
    ) -> HydraSelectState:
        expected = self.next_arguments()
        if (
            self.pending_call_id != call_id
            or self.pending_arguments != expected
            or dict(arguments) != expected
            or self.target_session_id not in {None, target_session_id}
        ):
            raise ValueError("Hydra-Select acknowledged target effect drifted from selection")
        observation, observation_matched, uncertainties = _objective_observation(
            exchange,
            oracle_digest=contract.objective_oracle_digest,
            adapter_digest=contract.reviewed_adapter_digest,
            modality=contract.modality,
            language=contract.language,
        )
        exchange_id = exchange.get("id")
        if not isinstance(exchange_id, (str, int)) or str(exchange_id).strip() == "":
            uncertainties = (
                *uncertainties,
                _evaluation_uncertain(
                    "target exchange identity metadata was absent", source="hydra.oracle"
                ),
            )
            exchange_id = _digest(exchange)[7:23]
        candidate_id = self.plan.selected_candidate_ids[len(self.steps)]  # type: ignore[union-attr]
        step = HydraStep(
            index=len(self.steps),
            scheduler_attempt_id=self.attempt_id,
            candidate_id=candidate_id,
            call_id=call_id,
            arguments_digest=_digest(arguments),
            exchange_digest=_digest(exchange),
            exchange_id=f"{call_id}:{exchange_id}",
            objective_observation_digest=_observation_digest(observation),
            objective_matched=observation_matched,
            target_reply_digest=_digest(exchange.get("response")),
            tool_content=tool_content,
        )
        steps = (*self.steps, step)
        return replace(
            self,
            target_session_id=target_session_id,
            steps=steps,
            pending_call_id=None,
            pending_arguments=None,
            pending_arguments_digest=None,
            evaluation_uncertainties=(*self.evaluation_uncertainties, *uncertainties),
        )

    def terminate(
        self, reason: Literal["cancelled", "capped", "blocked", "ambiguous"]
    ) -> HydraSelectState:
        if self.terminal_reason is not None:
            return self
        return replace(
            self,
            pending_call_id=None,
            pending_arguments=None,
            pending_arguments_digest=None,
            terminal_reason=reason,
        )

    def dump(self) -> dict[str, object]:
        return asdict(self)


def _canonical_prompt(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def _sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and value.startswith("sha256:")
        and len(value) == 71
        and all(character in "0123456789abcdef" for character in value[7:])
    )


def _digest(value: object) -> str:
    return (
        "sha256:"
        + hashlib.sha256(
            json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
    )


def _valid_observation(value: Mapping[str, object]) -> bool:
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
        and value.get("observation_digest") == _digest(body)
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
        tuple(_evaluation_uncertain(reason, source="hydra.oracle") for reason in reasons),
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
