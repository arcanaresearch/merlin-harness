from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from time import perf_counter
from types import SimpleNamespace
from urllib.parse import unquote, urlsplit

from merlin_agents import (
    AssignmentBudget,
    WorkerAssignment,
    WorkerHandoff,
    WorkerProfile,
    WorkerRuntime,
)
from merlin_agents.strategy_controller import propose_strategy_action
from merlin_contracts import (
    EvaluationMetrics,
    ModelCompleted,
    ModelError,
    PromotionCandidate,
    TextDelta,
    ToolCall,
    ToolResult,
    ToolSpec,
)

from .provider_evaluator import _budget_int, _read_owned_file
from .service import EvaluationBinding, EvaluationCase

CONTROLLER_STRATEGIES = frozenset({"goat", "crescendo", "hydra-select"})
_TARGET_TOOL = "target_request"
_CRESCENDO_PURPOSES = ("objective", "refusal", "progress", "invalid-premise")


@dataclass(frozen=True, slots=True)
class ControllerTargetReply:
    content: str
    observations: Mapping[str, bool]


@dataclass(frozen=True, slots=True)
class ControllerExpectedOutcome:
    status: str
    proposal_action: str
    proposal_next_stage: str | None
    observations: int
    target_calls: int
    attacker_requests: int | None
    objective_observed: bool
    error_marker: str


@dataclass(frozen=True, slots=True)
class ControllerEvaluationFixture:
    strategy_id: str
    contract: Mapping[str, str]
    attacker_turns: tuple[tuple[dict[str, object], ...], ...]
    target_replies: tuple[ControllerTargetReply, ...]
    context_window_tokens: int | None
    max_model_turns: int
    expected: ControllerExpectedOutcome
    evidence_markers: tuple[str, ...]


def load_controller_evaluation_fixture(
    case: EvaluationCase, *, max_bytes: int = 1_048_576
) -> ControllerEvaluationFixture:
    if case.resolved_fixture_path is not None:
        path = case.resolved_fixture_path
    else:
        parsed = urlsplit(case.target_fixture)
        if parsed.scheme != "file" or parsed.netloc not in {"", "localhost"}:
            raise ValueError("controller evaluation fixture must use a local file reference")
        path = Path(unquote(parsed.path))
    if not path.is_absolute():
        raise ValueError("controller evaluation fixture must resolve to an absolute path")
    payload = _read_owned_file(path, max_bytes=max_bytes)
    if f"sha256:{sha256(payload).hexdigest()}" != case.fixture_digest:
        raise ValueError("controller evaluation fixture digest mismatch")
    try:
        value = json.loads(payload.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as error:
        raise ValueError("controller evaluation fixture is not valid UTF-8 JSON") from error
    expected_keys = {
        "schema_version",
        "strategy_id",
        "contract",
        "attacker_turns",
        "target_replies",
        "context_window_tokens",
        "max_model_turns",
        "expected",
        "evidence_markers",
    }
    if not isinstance(value, dict) or set(value) != expected_keys:
        raise ValueError("controller evaluation fixture has an unexpected schema")
    schema_version = value["schema_version"]
    if schema_version not in {
        "merlin.controller-evaluation.v1",
        "merlin.controller-evaluation.v2",
    }:
        raise ValueError("controller evaluation fixture has an unsupported schema version")
    strategy_id = value["strategy_id"]
    if strategy_id not in CONTROLLER_STRATEGIES:
        raise ValueError("controller evaluation strategy is unsupported")
    contract = value["contract"]
    if (
        not isinstance(contract, dict)
        or any(not isinstance(key, str) or not key for key in contract)
        or any(not isinstance(item, str) or not item.strip() for item in contract.values())
        or not {"version", "semantic_technique_id"} <= set(contract)
    ):
        raise TypeError("controller evaluation contract must be a non-empty string mapping")
    attacker_turns = _attacker_turns(value["attacker_turns"])
    replies = _target_replies(value["target_replies"], strategy_id, schema_version)
    window = value["context_window_tokens"]
    if window is not None and (
        isinstance(window, bool) or not isinstance(window, int) or not 1_024 <= window <= 10_000_000
    ):
        raise ValueError("controller evaluation context_window_tokens is invalid")
    max_model_turns = value["max_model_turns"]
    if (
        isinstance(max_model_turns, bool)
        or not isinstance(max_model_turns, int)
        or not 1 <= max_model_turns <= 64
    ):
        raise ValueError("controller evaluation max_model_turns must be between 1 and 64")
    markers = value["evidence_markers"]
    if not isinstance(markers, list) or any(
        not isinstance(item, str) or not item for item in markers
    ):
        raise TypeError("controller evaluation evidence_markers must be a string array")
    return ControllerEvaluationFixture(
        strategy_id=strategy_id,
        contract=dict(contract),
        attacker_turns=attacker_turns,
        target_replies=replies,
        context_window_tokens=window,
        max_model_turns=max_model_turns,
        expected=_expected(
            value["expected"],
            schema_version=schema_version,
            strategy_id=strategy_id,
            observations=len(replies),
        ),
        evidence_markers=tuple(markers),
    )


class ControllerCaseEvaluator:
    """Replay one digest-bound adaptive-controller fixture through the production runtime.

    The scripted attacker stands in for the routed model and the fixture target
    stands in for the reviewed adapter; every target reply carries oracle
    observations rebound to the exact contract digests, so a controller only
    scores when it ran (strategy binding), respected its protocol invariants,
    and produced oracle-backed outcomes.
    """

    async def __call__(
        self,
        candidate: PromotionCandidate,
        case: EvaluationCase,
        binding: EvaluationBinding,
    ) -> EvaluationMetrics:
        if candidate.kind != "routing":
            raise ValueError("controller evaluation suites accept routing candidates only")
        expected_route = {
            "provider": binding.provider,
            "model": binding.model,
            "route": binding.route,
        }
        if any(candidate.content.get(key) != value for key, value in expected_route.items()):
            raise ValueError("routing candidate does not match the evaluation binding")
        fixture = load_controller_evaluation_fixture(case)
        if candidate.content.get("strategy_id") != fixture.strategy_id:
            raise ValueError("routing candidate does not bind the fixture controller strategy")
        timeout = _budget_int(binding, "case_timeout_seconds", default=120, minimum=1, maximum=900)
        started = perf_counter()
        async with asyncio.timeout(timeout):
            observed = await _replay(fixture, case, binding)
        latency_ms = max(0, int((perf_counter() - started) * 1_000))
        return _score(fixture, observed, latency_ms)


@dataclass(frozen=True, slots=True)
class _Observed:
    status: str
    error: str
    proposal_action: str
    proposal_next_stage: str | None
    observations: int
    objective_observed: bool
    target_calls: int
    attacker_requests: int
    markers_present: bool


def _score(
    fixture: ControllerEvaluationFixture, observed: _Observed, latency_ms: int
) -> EvaluationMetrics:
    expected = fixture.expected
    status_ok = observed.status == expected.status
    proposal_ok = (
        observed.proposal_action == expected.proposal_action
        and observed.proposal_next_stage == expected.proposal_next_stage
        and observed.observations == expected.observations
    )
    calls_ok = observed.target_calls == expected.target_calls
    requests_ok = (
        expected.attacker_requests is None
        or observed.attacker_requests == expected.attacker_requests
    )
    objective_ok = observed.objective_observed == expected.objective_observed
    marker_ok = not expected.error_marker or expected.error_marker in observed.error
    succeeded = (
        status_ok
        and proposal_ok
        and calls_ok
        and requests_ok
        and objective_ok
        and marker_ok
        and observed.markers_present
    )
    if expected.target_calls:
        attribution = min(observed.target_calls, expected.target_calls) / expected.target_calls
    else:
        attribution = float(observed.target_calls == 0)
    if not observed.markers_present:
        attribution = 0.0
    violations = max(0, observed.target_calls - expected.target_calls)
    if expected.attacker_requests is not None:
        violations += max(0, observed.attacker_requests - expected.attacker_requests)
    return EvaluationMetrics(
        task_success_rate=float(succeeded),
        false_positive_rate=float(observed.objective_observed and not expected.objective_observed),
        evidence_attribution_rate=attribution,
        authority_violations=violations,
        mean_cost=0,
        p95_latency_ms=latency_ms,
        cancellation_recovery_rate=float(proposal_ok),
    )


async def _replay(
    fixture: ControllerEvaluationFixture, case: EvaluationCase, binding: EvaluationBinding
) -> _Observed:
    provider = _ScriptedAttacker(fixture.attacker_turns, fixture.context_window_tokens)
    tools = _FixtureTargetTools(fixture)
    profile = WorkerProfile(
        "lead",
        1,
        "controller evaluation",
        "Run the reviewed adaptive controller exactly as the daemon directs.",
        frozenset({"controller"}),
    )
    assignment = WorkerAssignment(
        id=f"controller-eval-{case.id}",
        objective=f"Evaluate the {fixture.strategy_id} controller against its fixture target.",
        profile_id=profile.id,
        target_profile_id="controller-fixture-target",
        technique_ids=("t1",),
        guidance_ids=(),
        tool_ids=(_TARGET_TOOL,),
        skill_ids=(),
        provider_id=binding.provider,
        authority_ids=("auth-1",),
        budget=AssignmentBudget(max_turns=64, max_tokens=1_000_000, max_seconds=120),
        input_schema='{"type":"object"}',
        output_schema='{"type":"object","required":["finding"]}',
        provider_model=binding.model,
        semantic_technique_id=fixture.contract["semantic_technique_id"],
        strategy_id=fixture.strategy_id,
        strategy_contract=tuple(sorted(fixture.contract.items())),
    )
    handoff = WorkerHandoff(
        f"controller-eval-{case.id}",
        "/workspace",
        binding.model,
        profile,
        assignment,
        attempt_id=f"attempt-{case.id}",
    )
    checkpoints: list[Mapping[str, object]] = []

    async def checkpoint_writer(value: Mapping[str, object], _consumed_ids: object) -> None:
        checkpoints.append(value)

    result = await WorkerRuntime(
        provider,
        tools,
        max_model_turns=fixture.max_model_turns,
        checkpoint_writer=checkpoint_writer,
    ).run(handoff)
    strategy_state = next(
        (
            checkpoint["strategy_state"]
            for checkpoint in reversed(checkpoints)
            if isinstance(checkpoint.get("strategy_state"), Mapping)
        ),
        None,
    )
    steps = strategy_state.get("steps", ()) if isinstance(strategy_state, Mapping) else ()
    if fixture.strategy_id == "crescendo":
        objective = any(
            isinstance(step, Mapping) and step.get("decision") == "success" for step in steps
        )
    else:
        objective = any(
            isinstance(step, Mapping) and step.get("objective_matched") is True for step in steps
        )
    proposal = propose_strategy_action(
        fixture.strategy_id,
        strategy_state if isinstance(strategy_state, Mapping) else None,
        fixture.contract,
    )
    evidence = json.dumps(
        {
            "strategy_state": strategy_state,
            "strategy_proposal": proposal.dump(),
            "content": result.content,
            "structured": result.structured,
            "error": result.error,
        },
        sort_keys=True,
        default=str,
    )
    return _Observed(
        status=result.status,
        error=result.error or "",
        proposal_action=proposal.action,
        proposal_next_stage=proposal.next_stage,
        observations=proposal.observations,
        objective_observed=objective,
        target_calls=len(tools.calls),
        attacker_requests=len(provider.requests),
        markers_present=all(marker in evidence for marker in fixture.evidence_markers),
    )


class _ScriptedAttacker:
    def __init__(
        self,
        turns: tuple[tuple[dict[str, object], ...], ...],
        context_window_tokens: int | None,
    ) -> None:
        self._turns = [list(turn) for turn in turns]
        self.requests: list[object] = []
        self.capabilities = SimpleNamespace(
            vision=False, context_window_tokens=context_window_tokens
        )

    async def stream(self, request):
        self.requests.append(request)
        if not self._turns:
            yield ModelError(
                code="fixture_exhausted",
                message="scripted attacker turns are exhausted",
                retryable=False,
            )
            return
        for event in self._turns.pop(0):
            if "tool_call" in event:
                call = event["tool_call"]
                yield ToolCall(
                    call_id=str(call["call_id"]),
                    name=str(call["name"]),
                    arguments=dict(call["arguments"]),  # type: ignore[arg-type]
                )
            elif "text" in event:
                yield TextDelta(text=str(event["text"]))
            else:
                yield ModelCompleted(finish_reason=str(event["completed"]))


class _FixtureTargetTools:
    def __init__(self, fixture: ControllerEvaluationFixture) -> None:
        self._fixture = fixture
        self._replies = list(fixture.target_replies)
        self.calls: list[str] = []
        self.arguments: list[object] = []

    def describe(self):
        return (
            ToolSpec(
                name=_TARGET_TOOL,
                description="controller evaluation fixture target",
                input_schema={"type": "object"},
            ),
        )

    def effect(self, name: str) -> str | None:
        return "target_network" if name == _TARGET_TOOL else None

    async def invoke(self, name: str, context: object, arguments: object) -> ToolResult:
        del context
        if name != _TARGET_TOOL or not self._replies:
            return ToolResult(content="fixture target replies are exhausted", is_error=True)
        reply = self._replies.pop(0)
        self.calls.append(name)
        self.arguments.append(arguments)
        observations = [
            _observation(self._fixture, purpose, matched)
            for purpose, matched in reply.observations.items()
        ]
        return ToolResult(
            content=reply.content,
            metadata={
                "target_session_id": "api:controller-eval-session",
                "target_exchanges": [
                    {
                        "id": len(self.calls),
                        "interaction": True,
                        "request": arguments,
                        "response": {"reply": reply.content},
                        "oracle_observations": observations,
                    }
                ],
            },
        )


def _observation(
    fixture: ControllerEvaluationFixture, purpose: str, matched: bool
) -> dict[str, object]:
    contract = fixture.contract
    if fixture.strategy_id == "goat":
        oracle_by_purpose = {"objective": contract["terminal_oracle_digest"]}
    elif fixture.strategy_id == "hydra-select":
        oracle_by_purpose = {"objective": contract["objective_oracle_digest"]}
    else:
        oracle_by_purpose = {
            "objective": contract["objective_oracle_digest"],
            "refusal": contract["refusal_oracle_digest"],
            "progress": contract["progress_oracle_digest"],
            "invalid-premise": contract["invalid_premise_oracle_digest"],
        }
    oracle_digest = oracle_by_purpose[purpose]
    binding = {
        "adapter_digest": contract["reviewed_adapter_digest"],
        "step_id": "interaction",
        "oracle_digest": oracle_digest,
        "pointer": "/reply",
        "match_values": ["validated"],
        "modality": "text",
        "turn": "multi",
        "language": contract["language"],
        "purpose": purpose,
    }
    body = {
        "schema": "merlin.api-oracle-observation.v2",
        **binding,
        "binding_digest": _digest(binding),
        "matched": matched,
    }
    return {**body, "observation_digest": _digest(body)}


def _digest(value: object) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _attacker_turns(value: object) -> tuple[tuple[dict[str, object], ...], ...]:
    if not isinstance(value, list) or not value:
        raise TypeError("controller evaluation attacker_turns must be a non-empty array")
    turns: list[tuple[dict[str, object], ...]] = []
    for turn in value:
        if not isinstance(turn, list) or not turn:
            raise TypeError("controller evaluation attacker turn must be a non-empty array")
        events: list[dict[str, object]] = []
        for event in turn:
            if not isinstance(event, dict) or len(event) != 1:
                raise ValueError("controller evaluation attacker event has an unexpected schema")
            if "tool_call" in event:
                call = event["tool_call"]
                if not isinstance(call, dict) or set(call) != {"call_id", "name", "arguments"}:
                    raise ValueError("controller evaluation tool call has an unexpected schema")
                if (
                    not isinstance(call["call_id"], str)
                    or not call["call_id"].strip()
                    or not isinstance(call["name"], str)
                    or not call["name"].strip()
                    or not isinstance(call["arguments"], dict)
                ):
                    raise TypeError("controller evaluation tool call is invalid")
            elif "text" in event:
                if not isinstance(event["text"], str):
                    raise TypeError("controller evaluation text event must be a string")
            elif "completed" in event:
                if event["completed"] not in {"stop", "tool_calls", "length"}:
                    raise ValueError("controller evaluation completion reason is unsupported")
            else:
                raise ValueError("controller evaluation attacker event has an unexpected schema")
            events.append(event)
        turns.append(tuple(events))
    return tuple(turns)


def _target_replies(
    value: object, strategy_id: str, schema_version: object
) -> tuple[ControllerTargetReply, ...]:
    if not isinstance(value, list):
        raise TypeError("controller evaluation target_replies must be an array")
    purposes = ("objective",) if strategy_id != "crescendo" else _CRESCENDO_PURPOSES
    observation_key = (
        "matched" if schema_version == "merlin.controller-evaluation.v1" else "observations"
    )
    replies: list[ControllerTargetReply] = []
    for item in value:
        if not isinstance(item, dict) or set(item) != {"content", observation_key}:
            raise ValueError("controller evaluation target reply has an unexpected schema")
        if not isinstance(item["content"], str):
            raise TypeError("controller evaluation target reply content must be a string")
        observations = item[observation_key]
        if (
            not isinstance(observations, dict)
            or set(observations) != set(purposes)
            or any(type(flag) is not bool for flag in observations.values())
        ):
            raise ValueError("controller evaluation target reply observations are invalid")
        replies.append(
            ControllerTargetReply(content=item["content"], observations=dict(observations))
        )
    return tuple(replies)


def _expected(
    value: object,
    *,
    schema_version: object,
    strategy_id: str,
    observations: int,
) -> ControllerExpectedOutcome:
    if schema_version == "merlin.controller-evaluation.v1":
        return _legacy_expected(value, strategy_id=strategy_id, observations=observations)
    if not isinstance(value, dict) or set(value) != {
        "status",
        "proposal_action",
        "proposal_next_stage",
        "observations",
        "target_calls",
        "attacker_requests",
        "objective_observed",
        "error_marker",
    }:
        raise ValueError("controller evaluation expected outcome has an unexpected schema")
    if value["status"] not in {"completed", "failed"}:
        raise ValueError("controller evaluation expected status is unsupported")
    if value["proposal_action"] not in {"continue", "validate", "retry", "stop"}:
        raise ValueError("controller evaluation proposal_action is unsupported")
    next_stage = value["proposal_next_stage"]
    if next_stage is not None and (not isinstance(next_stage, str) or not next_stage.strip()):
        raise TypeError("controller evaluation proposal_next_stage must be null or non-empty")
    observations = value["observations"]
    if isinstance(observations, bool) or not isinstance(observations, int) or observations < 0:
        raise ValueError("controller evaluation observations must be a non-negative integer")
    target_calls = value["target_calls"]
    if isinstance(target_calls, bool) or not isinstance(target_calls, int) or target_calls < 0:
        raise ValueError("controller evaluation target_calls must be a non-negative integer")
    attacker_requests = value["attacker_requests"]
    if attacker_requests is not None and (
        isinstance(attacker_requests, bool)
        or not isinstance(attacker_requests, int)
        or attacker_requests < 0
    ):
        raise ValueError("controller evaluation attacker_requests must be null or non-negative")
    if type(value["objective_observed"]) is not bool:
        raise TypeError("controller evaluation objective_observed must be boolean")
    if not isinstance(value["error_marker"], str):
        raise TypeError("controller evaluation error_marker must be a string")
    if value["status"] == "failed" and not value["error_marker"]:
        raise ValueError("controller evaluation failures require an error marker")
    return ControllerExpectedOutcome(
        status=value["status"],
        proposal_action=value["proposal_action"],
        proposal_next_stage=next_stage,
        observations=observations,
        target_calls=target_calls,
        attacker_requests=attacker_requests,
        objective_observed=value["objective_observed"],
        error_marker=value["error_marker"],
    )


def _legacy_expected(
    value: object, *, strategy_id: str, observations: int
) -> ControllerExpectedOutcome:
    """Normalize frozen v1 terminal markers into observation-first proposals."""

    if not isinstance(value, dict) or set(value) != {
        "status",
        "terminal_reason",
        "target_calls",
        "attacker_requests",
        "objective_matched",
        "error_marker",
    }:
        raise ValueError("controller evaluation expected outcome has an unexpected schema")
    terminal = value["terminal_reason"]
    action_by_terminal = {
        "cancelled": "stop",
        "capped": "stop",
        "ambiguous": "retry",
        "blocked": "retry",
        "awaiting-judge": "validate",
        "positive": "validate",
        "invalid-premise": "validate",
        "plateau": "validate",
    }
    action = action_by_terminal.get(terminal, "continue")
    next_stage = None
    if action == "continue":
        next_stage = {
            "goat": "target-turn",
            "crescendo": "probe" if observations == 0 else "escalate",
            "hydra-select": "propose" if observations == 0 else "selected-candidate",
        }[strategy_id]
    normalized = {
        "status": value["status"],
        "proposal_action": action,
        "proposal_next_stage": next_stage,
        "observations": observations,
        "target_calls": value["target_calls"],
        "attacker_requests": value["attacker_requests"],
        "objective_observed": value["objective_matched"],
        "error_marker": value["error_marker"],
    }
    return _expected(
        normalized,
        schema_version="merlin.controller-evaluation.v2",
        strategy_id=strategy_id,
        observations=observations,
    )
