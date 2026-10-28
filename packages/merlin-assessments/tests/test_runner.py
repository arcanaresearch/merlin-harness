from collections.abc import AsyncIterator

from merlin_assessments import AssessmentProgress, AssessmentRunner
from merlin_contracts import (
    AttackCase,
    AttackPack,
    ChatRequest,
    ModelCompleted,
    ModelError,
    ModelEvent,
    TextDelta,
)


class ScriptedProvider:
    def __init__(self, replies: list[str]) -> None:
        self.replies = iter(replies)
        self.requests: list[ChatRequest] = []

    async def stream(self, request: ChatRequest) -> AsyncIterator[ModelEvent]:
        self.requests.append(request)
        for word in next(self.replies).split("|"):
            yield TextDelta(text=word)
        yield ModelCompleted(finish_reason="stop")


def pack(turn_limit: int = 2) -> tuple[AttackPack, AttackCase]:
    case = AttackCase(
        id="case-1",
        title="Test case",
        instructions="Probe the target.",
        objectives=["Obtain the marker"],
        turn_limit=turn_limit,
    )
    return (
        AttackPack(
            id="test-pack",
            version="1",
            title="Test pack",
            target_kind="ai-chat",
            instructions="Stay within scope.",
            cases=[case],
        ),
        case,
    )


async def test_runs_bounded_alternating_stream() -> None:
    attacker = ScriptedProvider(["attack| one", "attack two"])
    target = ScriptedProvider(["target one", "target two"])
    progress: list[AssessmentProgress] = []

    async def capture(event: AssessmentProgress) -> None:
        progress.append(event)

    runner = AssessmentRunner(
        attacker=attacker,
        target=target,
        attacker_model="attacker-model",
        target_model="target-model",
        on_progress=capture,
    )
    attack_pack, case = pack()
    result = await runner.run(
        attack_pack, case, session_id="session-1", target_profile_id="target-1"
    )

    assert result.run.status == "completed"
    assert [turn.role for turn in result.turns] == ["attacker", "target"] * 2
    assert [turn.sequence for turn in result.turns] == [0, 1, 2, 3]
    assert len(attacker.requests) == len(target.requests) == 2
    assert {request.workload for request in attacker.requests} == {"assessment_attacker"}
    assert {request.workload for request in target.requests} == {"assessment_target"}
    assert attacker.requests[1].messages[-1].content == "The target replied:\n\ntarget one"
    assert [event.kind for event in progress].count("text_delta") == 5
    assert progress[0].kind == "run_started"
    assert progress[-1].kind == "run_completed"
    assert result.outcome.summary.endswith("not scored.")


async def test_provider_error_returns_failed_partial_run() -> None:
    class FailingProvider:
        async def stream(self, request: ChatRequest) -> AsyncIterator[ModelEvent]:
            yield ModelError(message="unavailable", retryable=True)

    attack_pack, case = pack(turn_limit=1)
    runner = AssessmentRunner(
        attacker=ScriptedProvider(["hello"]),
        target=FailingProvider(),
        attacker_model="a",
        target_model="t",
    )

    result = await runner.run(
        attack_pack, case, session_id="session-1", target_profile_id="target-1"
    )

    assert result.run.status == "failed"
    assert [turn.role for turn in result.turns] == ["attacker"]
    assert result.outcome.summary == "target provider failed: unavailable"


async def test_rejects_case_from_another_pack() -> None:
    import pytest

    attack_pack, _ = pack()
    other_case = AttackCase(id="other", title="Other", instructions="Other")
    runner = AssessmentRunner(
        attacker=ScriptedProvider([]),
        target=ScriptedProvider([]),
        attacker_model="a",
        target_model="t",
    )

    with pytest.raises(ValueError, match="does not belong"):
        await runner.run(
            attack_pack,
            other_case,
            session_id="session-1",
            target_profile_id="target-1",
        )
