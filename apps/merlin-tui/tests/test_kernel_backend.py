import asyncio
import json
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from merlin_agents.swarm_snapshot import (
    AssignmentSnapshot,
    AttemptLifecycle,
    CandidateAxes,
    CandidateEvaluation,
    CandidateEvaluationState,
    CurrentAttempt,
    IdentityProvenance,
    NodeLifecycle,
    SpecialistSnapshot,
    SuccessfulResultSummary,
    SwarmSnapshot,
)
from merlin_config import Config, ConfigPaths, load_config
from merlin_contracts import (
    AgentSummary,
    AppEvent,
    AttemptProjection,
    ChatMessage,
    Finding,
    LeaseProjection,
    NodeProjection,
    ScheduleProjection,
)
from merlin_tui.configuration import ProviderConfiguration
from merlin_tui.kernel_backend import KernelBackend, KernelSettings, event_to_stream
from merlin_tui.operator_tools import campaign_submit_envelope


class FakeKernel:
    def __init__(self, events: list[AppEvent]) -> None:
        self.events = events
        self.submissions: list[str] = []
        self.turn_active = False
        self.session_id = "session-1"

    async def submit(self, text: str) -> None:
        self.submissions.append(text)

    async def next_event(self) -> AppEvent:
        if self.events:
            return self.events.pop(0)
        await asyncio.sleep(1)
        raise AssertionError("unreachable")

    async def interrupt(self) -> bool:
        return True

    async def start(self, session_id: str | None = None) -> str:
        self.session_id = session_id or self.session_id
        return self.session_id


@pytest.mark.asyncio
async def test_close_is_idempotent_and_closes_all_resources_after_failure() -> None:
    calls = []

    class Resource:
        def __init__(self, name: str, fail: bool = False) -> None:
            self.name = name
            self.fail = fail

        def close(self) -> None:
            calls.append(self.name)
            if self.fail:
                raise RuntimeError(self.name)

    backend = KernelBackend(  # type: ignore[arg-type]
        FakeKernel([]), (Resource("database"), Resource("connection", fail=True))
    )

    with pytest.raises(ExceptionGroup, match="backend shutdown failed"):
        await backend.close()
    await backend.close()
    assert calls == ["connection", "database"]


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["close", "detach"])
async def test_backend_closes_owned_mcp_once_outside_resource_closer(method: str) -> None:
    class Mcp:
        def __init__(self) -> None:
            self.close_calls = 0

        async def close(self) -> None:
            self.close_calls += 1

    mcp = Mcp()
    backend = KernelBackend(FakeKernel([]), (mcp,), mcp=mcp)  # type: ignore[arg-type]

    await getattr(backend, method)()
    await getattr(backend, method)()

    assert mcp.close_calls == 1


@pytest.mark.asyncio
async def test_close_continues_resource_teardown_after_mcp_timeout() -> None:
    calls: list[str] = []

    class Mcp:
        tuning = SimpleNamespace(shutdown_timeout_seconds=0.01)

        async def close(self) -> None:
            calls.append("mcp:close")
            await asyncio.sleep(10)

    class Resource:
        def close(self) -> None:
            calls.append("resource:close")

    backend = KernelBackend(  # type: ignore[arg-type]
        FakeKernel([]),
        (Resource(),),
        mcp=Mcp(),
    )

    with pytest.raises(ExceptionGroup, match="backend shutdown failed") as caught:
        await backend.close()

    assert any(isinstance(error, TimeoutError) for error in caught.value.exceptions)
    assert calls == ["mcp:close", "resource:close"]


@pytest.mark.asyncio
async def test_close_pauses_only_current_session_campaigns_before_resource_shutdown() -> None:
    paused: list[str] = []

    class Campaigns:
        async def list_all(self):
            return (
                {
                    "campaign_id": "campaign/current",
                    "session_id": "session-1",
                    "status": "executing",
                },
                {
                    "campaign_id": "campaign/other",
                    "session_id": "session-2",
                    "status": "executing",
                },
                {
                    "campaign_id": "campaign/done",
                    "session_id": "session-1",
                    "status": "completed",
                },
            )

        async def pause(self, campaign_id: str) -> None:
            paused.append(campaign_id)

    backend = KernelBackend(FakeKernel([]), campaigns=Campaigns())  # type: ignore[arg-type]

    await backend.close()

    assert paused == ["campaign/current"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("statuses", "expected"),
    [
        (("completed", "paused"), False),
        (("completed", "executing"), True),
    ],
)
async def test_active_background_work_reads_daemon_wide_campaign_state(
    statuses: tuple[str, ...], expected: bool
) -> None:
    class Campaigns:
        async def list_all(self):
            return tuple(
                {
                    "campaign_id": f"campaign/{index}",
                    "session_id": f"session-{index}",
                    "status": status,
                }
                for index, status in enumerate(statuses)
            )

    backend = KernelBackend(FakeKernel([]), campaigns=Campaigns())  # type: ignore[arg-type]

    assert await backend.has_active_background_work() is expected


@pytest.mark.asyncio
async def test_close_continues_teardown_when_one_campaign_pause_fails() -> None:
    calls: list[str] = []

    class Campaigns:
        async def list_all(self):
            return (
                {
                    "campaign_id": "campaign/fails",
                    "session_id": "session-1",
                    "status": "executing",
                },
                {
                    "campaign_id": "campaign/continues",
                    "session_id": "session-1",
                    "status": "executing",
                },
            )

        async def pause(self, campaign_id: str) -> None:
            calls.append(f"pause:{campaign_id}")
            if campaign_id == "campaign/fails":
                raise RuntimeError("durable pause failed")

        async def detach(self) -> None:
            calls.append("campaigns:detach")

    class Agents:
        async def detach(self) -> None:
            calls.append("agents:detach")

    class Resource:
        def close(self) -> None:
            calls.append("resource:close")

    backend = KernelBackend(  # type: ignore[arg-type]
        FakeKernel([]),
        (Resource(),),
        campaigns=Campaigns(),
        agents=Agents(),
    )

    with pytest.raises(ExceptionGroup, match="backend shutdown failed"):
        await backend.close()

    assert calls == [
        "pause:campaign/fails",
        "pause:campaign/continues",
        "campaigns:detach",
        "agents:detach",
        "resource:close",
    ]


@pytest.mark.asyncio
async def test_resume_restores_only_paused_campaigns_owned_by_selected_session() -> None:
    resumed: list[str] = []

    class Campaigns:
        async def recover(self, *, resume_execution=True) -> None:
            return None

        async def list_all(self):
            return (
                {
                    "objective": "Saved assessment",
                    "profile": "recommended",
                    "manifest_digest": "digest",
                    "campaign_id": "campaign/current",
                    "session_id": "session-1",
                    "status": "paused",
                },
                {
                    "objective": "Saved assessment",
                    "profile": "recommended",
                    "manifest_digest": "digest",
                    "campaign_id": "campaign/other",
                    "session_id": "session-2",
                    "status": "paused",
                },
                {
                    "objective": "Saved assessment",
                    "profile": "recommended",
                    "manifest_digest": "digest",
                    "campaign_id": "campaign/done",
                    "session_id": "session-1",
                    "status": "completed",
                },
            )

        async def list(self, session_id, *, limit):
            assert limit is None
            return tuple(row for row in await self.list_all() if row["session_id"] == session_id)

        async def resume(self, campaign_id: str) -> None:
            resumed.append(campaign_id)

    backend = KernelBackend(FakeKernel([]), campaigns=Campaigns())  # type: ignore[arg-type]

    events = [event async for event in backend.stream("/resume session-1")]

    assert resumed == ["campaign/current"]
    assert any("Resumed 1 paused campaign" in event.text for event in events)


@pytest.mark.asyncio
async def test_resume_reports_campaign_reconciliation_failure() -> None:
    class Campaigns:
        async def recover(self, *, resume_execution=True) -> None:
            return None

        async def list_all(self):
            return (
                {
                    "objective": "Saved assessment",
                    "profile": "recommended",
                    "manifest_digest": "digest",
                    "campaign_id": "campaign/stale-wave",
                    "session_id": "session-1",
                    "status": "paused",
                },
            )

        async def list(self, session_id, *, limit):
            assert limit is None
            return tuple(row for row in await self.list_all() if row["session_id"] == session_id)

        async def resume(self, _campaign_id: str) -> None:
            raise ValueError("terminal scheduler continuation could not be reconciled")

    backend = KernelBackend(FakeKernel([]), campaigns=Campaigns())  # type: ignore[arg-type]

    events = [event async for event in backend.stream("/resume session-1")]

    assert any(
        event.text
        == (
            "Could not resume campaign/stale-wave: "
            "terminal scheduler continuation could not be reconciled"
        )
        for event in events
    )


@pytest.mark.asyncio
async def test_explicit_campaign_resume_rejects_a_different_session() -> None:
    resumed: list[str] = []

    class Campaigns:
        async def list_all(self):
            return (
                {
                    "campaign_id": "campaign/other",
                    "session_id": "session-2",
                    "status": "paused",
                },
            )

        async def resume(self, campaign_id: str) -> None:
            resumed.append(campaign_id)

    backend = KernelBackend(FakeKernel([]), campaigns=Campaigns())  # type: ignore[arg-type]

    await backend.resume_campaign("campaign/other")

    assert backend.kernel.session_id == "session-2"
    assert resumed == ["campaign/other"]


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ("pause_campaign",))
async def test_campaign_operations_reject_a_different_session(operation: str) -> None:
    calls: list[str] = []

    class Campaigns:
        async def list_all(self):
            return (
                {
                    "campaign_id": "campaign/foreign",
                    "session_id": "session-2",
                },
            )

        async def status(self, campaign_id: str):
            calls.append(f"status:{campaign_id}")
            return {}

        async def pause(self, campaign_id: str):
            calls.append(f"pause:{campaign_id}")

    backend = KernelBackend(FakeKernel([]), campaigns=Campaigns())  # type: ignore[arg-type]

    with pytest.raises(ValueError, match="different session"):
        await getattr(backend, operation)("campaign/foreign")

    assert calls == []


@pytest.mark.asyncio
async def test_campaign_status_reads_a_different_session_without_switching() -> None:
    calls: list[str] = []

    class Campaigns:
        async def list_all(self):
            return (
                {
                    "campaign_id": "campaign/foreign",
                    "session_id": "session-2",
                },
            )

        async def status(self, campaign_id: str):
            calls.append(campaign_id)
            return {"campaign_id": campaign_id, "status": "paused"}

    backend = KernelBackend(FakeKernel([]), campaigns=Campaigns())  # type: ignore[arg-type]

    status = await backend.campaign_status("campaign/foreign")

    assert backend.kernel.session_id == "session-1"
    assert status == {"campaign_id": "campaign/foreign", "status": "paused"}
    assert calls == ["campaign/foreign"]


def app_event(type_: str, **payload: object) -> AppEvent:
    return AppEvent(type=type_, payload=payload, occurred_at=datetime.now(UTC))


@pytest.mark.asyncio
async def test_adapter_forwards_commands_and_maps_events() -> None:
    kernel = FakeKernel([app_event("command", text="Commands: /help")])
    backend = KernelBackend(kernel)  # type: ignore[arg-type]

    events = [event async for event in backend.stream("/help")]

    assert kernel.submissions == ["/help"]
    assert [(event.kind, event.text) for event in events] == [("status", "Commands: /help")]


@pytest.mark.asyncio
async def test_campaign_guidance_is_scoped_to_current_session() -> None:
    calls = []

    class Campaigns:
        async def guide(self, session_id: str, campaign_id: str, message: str):
            calls.append((session_id, campaign_id, message))
            return {"delivery": "next_lead_boundary"}

    backend = KernelBackend(FakeKernel([]), campaigns=Campaigns())  # type: ignore[arg-type]

    result = await backend.guide_campaign("campaign-1", "focus on auth boundary")

    assert result == {"delivery": "next_lead_boundary"}
    assert calls == [("session-1", "campaign-1", "focus on auth boundary")]


@pytest.mark.asyncio
async def test_campaign_profile_gate_precedes_provider_and_preserves_objective() -> None:
    kernel = FakeKernel([])
    backend = KernelBackend(kernel)  # type: ignore[arg-type]

    choices = [
        event
        async for event in backend.stream(
            "Run a Merlin scan on https://target.test for policy override"
        )
    ]

    assert kernel.submissions == []
    assert len(choices) == 1
    assert "1. Recommended [default]" in choices[0].text
    assert "3. Exhaustive" in choices[0].text
    assert "4. Exploration / learning" in choices[0].text
    campaign_request_id = backend._pending_campaign_request_id
    assert campaign_request_id is not None

    events = [event async for event in backend.stream("1")]

    assert events == []
    assert kernel.submissions == [
        "Run a Merlin scan on https://target.test for policy override\n"
        "The operator explicitly selected campaign profile: recommended; "
        "do not ask for the profile again."
        " Pass the operator objective text verbatim as the assessment_prepare "
        "objective: do not reword, paraphrase, or substitute the target line; "
        "Merlin anchors the reviewed target itself.\n"
        + campaign_submit_envelope(
            "Run a Merlin scan on https://target.test for policy override",
            campaign_request_id=campaign_request_id,
        )
    ]


@pytest.mark.asyncio
async def test_campaign_profile_gate_accepts_exploration_number() -> None:
    kernel = FakeKernel([])
    backend = KernelBackend(kernel)  # type: ignore[arg-type]
    _ = [event async for event in backend.stream("Run a merlin scan on https://target.test")]
    campaign_request_id = backend._pending_campaign_request_id
    assert campaign_request_id is not None

    events = [event async for event in backend.stream("4")]

    assert events == []
    assert kernel.submissions == [
        "Run a merlin scan on https://target.test\n"
        "The operator explicitly selected campaign profile: exploration; "
        "do not ask for the profile again."
        " Pass the operator objective text verbatim as the assessment_prepare "
        "objective: do not reword, paraphrase, or substitute the target line; "
        "Merlin anchors the reviewed target itself.\n"
        + campaign_submit_envelope(
            "Run a merlin scan on https://target.test",
            campaign_request_id=campaign_request_id,
        )
    ]


@pytest.mark.asyncio
async def test_explicit_profile_campaign_request_still_emits_typed_boundary() -> None:
    kernel = FakeKernel([])
    backend = KernelBackend(kernel, operator_controls=True)  # type: ignore[arg-type]
    prompt = "Assess https://target.test for prompt injection with campaign profile exhaustive"

    assert [event async for event in backend.stream(prompt)] == []

    submitted = kernel.submissions[0]
    assert submitted.startswith(prompt + "\nMERLIN_CAMPAIGN_SUBMIT_REQUEST\n")
    envelope = json.loads(submitted.split("MERLIN_CAMPAIGN_SUBMIT_REQUEST\n", 1)[1])
    assert envelope["schema_version"] == "merlin.cli-campaign-submit.v2"
    assert envelope["operator_objective"] == prompt
    assert envelope["campaign_request_id"]


@pytest.mark.asyncio
async def test_cli_typed_campaign_prompt_keeps_one_stable_backend_boundary() -> None:
    kernel = FakeKernel([])
    backend = KernelBackend(kernel, operator_controls=True)  # type: ignore[arg-type]
    objective = "Assess https://target.test"
    campaign_request_id = "00000000-0000-4000-8000-000000000040"
    prompt = (
        objective
        + "\n"
        + campaign_submit_envelope(
            objective,
            campaign_request_id=campaign_request_id,
        )
        + "\nThe operator explicitly selected campaign profile: exhaustive; "
        "do not ask for the profile again."
    )

    assert [event async for event in backend.stream(prompt)] == []
    assert [event async for event in backend.stream(prompt)] == []

    assert kernel.submissions == [prompt, prompt]
    for submission in kernel.submissions:
        assert submission.count("MERLIN_CAMPAIGN_SUBMIT_REQUEST\n") == 1
        envelope, _end = json.JSONDecoder().raw_decode(
            submission.split("MERLIN_CAMPAIGN_SUBMIT_REQUEST\n", 1)[1]
        )
        assert envelope["campaign_request_id"] == campaign_request_id


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("prompt", "adds_boundary"),
    [
        ("Resume the assessment.", False),
        ("Retry", False),
        ("Please resume", False),
        ("Retry https://a.test", False),
        ("Continue the assessment against https://b.test", True),
        ("Read the docs at https://b.test", False),
    ],
)
async def test_backend_continuation_is_target_aware(prompt: str, adds_boundary: bool) -> None:
    objective = "Assess https://a.test"
    request_id = "00000000-0000-4000-8000-000000000059"

    class Store:
        async def load_messages(self, session_id: str):
            assert session_id == "session-1"
            return (
                ChatMessage(
                    role="user",
                    content=campaign_submit_envelope(objective, campaign_request_id=request_id),
                ),
            )

    kernel = FakeKernel([])
    kernel._store = Store()
    backend = KernelBackend(kernel, operator_controls=True)  # type: ignore[arg-type]

    assert [event async for event in backend.stream(prompt)] == []

    submitted = kernel.submissions[0]
    assert (submitted != prompt) is adds_boundary
    assert ("MERLIN_CAMPAIGN_SUBMIT_REQUEST\n" in submitted) is adds_boundary


@pytest.mark.asyncio
async def test_backend_valid_typed_resume_prefix_is_never_double_wrapped() -> None:
    objective = "Continue assessing https://c.test"
    request_id = "00000000-0000-4000-8000-000000000060"
    prompt = campaign_submit_envelope(objective, campaign_request_id=request_id)
    kernel = FakeKernel([])
    backend = KernelBackend(kernel, operator_controls=True)  # type: ignore[arg-type]

    assert [event async for event in backend.stream(prompt)] == []

    assert kernel.submissions == [prompt]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("prompt", "fresh_boundary"),
    [
        ("Signed in. Hand the browser back and continue reconnaissance.", False),
        (
            "Signed in at https://a.test. Hand the browser back and continue reconnaissance.",
            False,
        ),
        (
            "Signed in at https://b.test. Hand the browser back and continue reconnaissance.",
            True,
        ),
        (
            "Signed in at https://a.test:bad. Hand the browser back and continue reconnaissance.",
            True,
        ),
        (
            "Assess https://a.test while logged in and continue testing for authorization bypass.",
            True,
        ),
    ],
)
async def test_backend_handback_continuation_is_target_aware(
    prompt: str, fresh_boundary: bool
) -> None:
    objective = "Assess https://a.test"
    request_id = "00000000-0000-4000-8000-000000000077"

    class Store:
        async def load_messages(self, _session_id: str):
            return (
                ChatMessage(
                    role="user",
                    content=campaign_submit_envelope(objective, campaign_request_id=request_id),
                ),
            )

    kernel = FakeKernel([])
    kernel._store = Store()
    backend = KernelBackend(kernel, operator_controls=True)  # type: ignore[arg-type]

    assert [event async for event in backend.stream(prompt)] == []

    submitted = kernel.submissions[0]
    assert ("MERLIN_CAMPAIGN_SUBMIT_REQUEST\n" in submitted) is fresh_boundary
    if fresh_boundary:
        envelope, _end = json.JSONDecoder().raw_decode(
            submitted.split("MERLIN_CAMPAIGN_SUBMIT_REQUEST\n", 1)[1]
        )
        assert envelope["campaign_request_id"] != request_id
        assert envelope["operator_objective"] == prompt
    else:
        assert submitted == prompt


@pytest.mark.asyncio
async def test_backend_upgrades_legacy_envelope_to_one_exact_v2_boundary() -> None:
    objective = "Resume the assessment against https://b.test"
    suffix = (
        "\nThe operator explicitly selected campaign profile: exhaustive; "
        "do not ask for the profile again. Budget: 12 turns. Composition: autonomous."
    )
    prompt = (
        objective
        + "\nMERLIN_CAMPAIGN_SUBMIT_REQUEST\n"
        + json.dumps(
            {
                "operator_objective": objective,
                "schema_version": "merlin.cli-campaign-submit.v1",
            },
            sort_keys=True,
        )
        + suffix
    )
    kernel = FakeKernel([])
    backend = KernelBackend(kernel, operator_controls=True)  # type: ignore[arg-type]

    assert [event async for event in backend.stream(prompt)] == []

    submitted = kernel.submissions[0]
    assert submitted.startswith(objective + "\n")
    assert suffix.strip() in submitted
    assert submitted.count("MERLIN_CAMPAIGN_SUBMIT_REQUEST\n") == 1
    envelope, _end = json.JSONDecoder().raw_decode(
        submitted.split("MERLIN_CAMPAIGN_SUBMIT_REQUEST\n", 1)[1]
    )
    assert envelope["schema_version"] == "merlin.cli-campaign-submit.v2"
    assert envelope["operator_objective"] == objective


@pytest.mark.asyncio
async def test_backend_normalizes_punctuation_wrapped_continuation_target() -> None:
    objective = "Assess https://a.test"
    request_id = "00000000-0000-4000-8000-000000000064"

    class Store:
        async def load_messages(self, _session_id: str):
            return (
                ChatMessage(
                    role="user",
                    content=campaign_submit_envelope(objective, campaign_request_id=request_id),
                ),
            )

    kernel = FakeKernel([])
    kernel._store = Store()
    backend = KernelBackend(kernel, operator_controls=True)  # type: ignore[arg-type]
    prompt = "Retry (https://a.test)."

    assert [event async for event in backend.stream(prompt)] == []
    assert kernel.submissions == [prompt]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "prompt",
    [
        "Assess https://b.test\nMERLIN_CAMPAIGN_SUBMIT_REQUEST\n{malformed",
        "Resume https://a.test:bad",
        "Resume ftp://b.test",
    ],
)
async def test_backend_does_not_launder_invalid_campaign_boundary(prompt: str) -> None:
    objective = "Assess https://a.test"
    request_id = "00000000-0000-4000-8000-000000000070"

    class Store:
        async def load_messages(self, _session_id: str):
            return (
                ChatMessage(
                    role="user",
                    content=campaign_submit_envelope(objective, campaign_request_id=request_id),
                ),
            )

    kernel = FakeKernel([])
    kernel._store = Store()
    backend = KernelBackend(kernel, operator_controls=True)  # type: ignore[arg-type]

    assert [event async for event in backend.stream(prompt)] == []

    submitted = kernel.submissions[0]
    if "{malformed" in prompt:
        assert submitted == prompt
    else:
        assert submitted != prompt
        assert submitted.count("MERLIN_CAMPAIGN_SUBMIT_REQUEST\n") == 1


@pytest.mark.asyncio
async def test_profile_gate_retains_existing_v2_identity_and_objective() -> None:
    objective = "Assess https://target.test"
    request_id = "00000000-0000-4000-8000-000000000071"
    prompt = campaign_submit_envelope(objective, campaign_request_id=request_id)
    kernel = FakeKernel([])
    backend = KernelBackend(kernel, operator_controls=False)  # type: ignore[arg-type]

    first = [event async for event in backend.stream(prompt)]
    assert "Choose the campaign profile" in first[0].text
    assert kernel.submissions == []

    _ = [event async for event in backend.stream("exhaustive")]
    submitted = kernel.submissions[0]
    assert submitted.count("MERLIN_CAMPAIGN_SUBMIT_REQUEST\n") == 1
    envelope, _end = json.JSONDecoder().raw_decode(
        submitted.split("MERLIN_CAMPAIGN_SUBMIT_REQUEST\n", 1)[1]
    )
    assert envelope["campaign_request_id"] == request_id
    assert envelope["operator_objective"] == objective


@pytest.mark.asyncio
async def test_campaign_route_preflight_blocks_before_provider_and_retries() -> None:
    kernel = FakeKernel([])
    attempts = 0

    async def preflight(profile: str) -> tuple[bool, str]:
        nonlocal attempts
        attempts += 1
        assert profile == "recommended"
        return (attempts > 1, "routes ready" if attempts > 1 else "lead route unavailable")

    backend = KernelBackend(kernel, route_preflight=preflight)  # type: ignore[arg-type]
    _ = [event async for event in backend.stream("Run a merlin scan on https://target.test")]
    campaign_request_id = backend._pending_campaign_request_id
    assert campaign_request_id is not None

    blocked = [event async for event in backend.stream("1")]
    assert kernel.submissions == []
    assert "lead route unavailable" in blocked[0].text
    assert "2. Retry preflight" in blocked[1].text

    retried = [event async for event in backend.stream("2")]
    assert retried[0].text == "routes ready"
    assert kernel.submissions == [
        "Run a merlin scan on https://target.test\n"
        "The operator explicitly selected campaign profile: recommended; "
        "do not ask for the profile again."
        " Pass the operator objective text verbatim as the assessment_prepare "
        "objective: do not reword, paraphrase, or substitute the target line; "
        "Merlin anchors the reviewed target itself.\n"
        + campaign_submit_envelope(
            "Run a merlin scan on https://target.test",
            campaign_request_id=campaign_request_id,
        )
    ]


@pytest.mark.asyncio
async def test_campaign_profile_applies_lead_route_without_persisting() -> None:
    config = Config(
        database_url="postgresql://localhost/merlin",
        values={
            "provider": {
                "role_models": {
                    "lead": "gpt-daybreak-blue-latest",
                    "routine": "deepseek-v4-flash",
                }
            }
        },
        sources={},
    )
    settings = KernelSettings(config, "deepseek-v4-flash")
    kernel = FakeKernel([])
    backend = KernelBackend(kernel, settings=settings)  # type: ignore[arg-type]

    _ = [event async for event in backend.stream("Run a merlin scan on https://target.test")]
    _ = [event async for event in backend.stream("recommended")]

    assert await settings.get_model() == "gpt-daybreak-blue-latest"
    _ = [event async for event in backend.stream("/new")]
    assert await settings.get_model() == "deepseek-v4-flash"
    assert backend._pending_campaign_request_id is None


@pytest.mark.asyncio
async def test_chat_campaign_prompt_honors_configured_autonomous_composition() -> None:
    config = Config(
        database_url="postgresql://localhost/merlin",
        values={"specialist_composition": "autonomous"},
        sources={},
    )
    settings = KernelSettings(config, "deepseek-v4-flash")
    kernel = FakeKernel([])
    backend = KernelBackend(kernel, settings=settings)  # type: ignore[arg-type]

    _ = [event async for event in backend.stream("Run a merlin scan on https://target.test")]
    campaign_request_id = backend._pending_campaign_request_id
    assert campaign_request_id is not None
    _ = [event async for event in backend.stream("1")]

    assert kernel.submissions == [
        "Run a merlin scan on https://target.test\n"
        "The operator explicitly selected campaign profile: recommended; "
        "do not ask for the profile again."
        "\nThe operator explicitly selected specialist composition: autonomous "
        "(orthogonal to the coverage profile); pass "
        "specialist_composition='autonomous' to assessment_prepare; do not ask again."
        " Pass the operator objective text verbatim as the assessment_prepare "
        "objective: do not reword, paraphrase, or substitute the target line; "
        "Merlin anchors the reviewed target itself.\n"
        + campaign_submit_envelope(
            "Run a merlin scan on https://target.test",
            campaign_request_id=campaign_request_id,
        )
    ]


@pytest.mark.asyncio
async def test_apply_reload_updates_specialist_composition() -> None:
    config = Config(
        database_url="postgresql://localhost/merlin",
        values={},
        sources={},
    )
    settings = KernelSettings(config, "deepseek-v4-flash")
    newer = Config(
        database_url="postgresql://localhost/merlin",
        values={"specialist_composition": "autonomous"},
        sources={"specialist_composition": "config"},
    )

    settings.apply_reload(newer, frozenset({"specialist_composition"}))

    assert settings.config.specialist_composition == "autonomous"


@pytest.mark.asyncio
async def test_new_session_clears_pending_campaign_profile_gate() -> None:
    kernel = FakeKernel([])
    backend = KernelBackend(kernel)  # type: ignore[arg-type]
    _ = [event async for event in backend.stream("Run a merlin scan on https://target.test")]

    _ = [event async for event in backend.stream("/new")]

    assert kernel.submissions == ["/new"]


@pytest.mark.asyncio
async def test_new_session_does_not_wait_for_mcp_startup() -> None:
    class BlockingMcp:
        async def start_configured(self) -> None:
            await asyncio.Event().wait()

    kernel = FakeKernel([])
    backend = KernelBackend(kernel, mcp=BlockingMcp())  # type: ignore[arg-type]

    async with asyncio.timeout(0.5):
        events = [event async for event in backend.stream("/new")]

    assert events == []
    assert kernel.submissions == ["/new"]
    assert not backend._mcp_started


def test_event_mapping_preserves_stream_and_failures() -> None:
    assert event_to_stream(app_event("stream", text="token")).kind == "content"
    usage = event_to_stream(
        app_event(
            "usage",
            input_tokens=808,
            output_tokens=23,
            total_tokens=831,
            estimated_cost_usd=None,
        )
    )
    assert usage is not None
    assert (usage.kind, usage.text) == ("usage", "831 tokens (808 in · 23 out)")
    failure = event_to_stream(app_event("tool", name="read_file", state="failed", text="denied"))
    assert failure is not None
    assert failure.kind == "tool"
    assert failure.text == "read_file failed: denied"
    assert event_to_stream(app_event("error", text="provider down")).kind == "error"
    assert event_to_stream(app_event("message", role="assistant", text="token")) is None


@pytest.mark.parametrize(
    ("state", "text", "result_label"),
    [
        ("started", "", None),
        ("completed", "done", "Result: done"),
        ("failed", "denied", "Error: denied"),
        ("interrupted", "stopped", "Error: stopped"),
    ],
)
def test_shell_events_show_the_reviewed_command_and_working_directory(
    state: str, text: str, result_label: str | None
) -> None:
    event = event_to_stream(
        app_event(
            "tool",
            name="shell_command",
            state=state,
            text=text,
            invocation={
                "command": "rg -n TODO packages",
                "working_directory": "/workspace/project",
            },
        )
    )

    assert event is not None
    assert event.kind == "tool"
    assert event.text.startswith("shell_command " + state + ": rg -n TODO packages")
    assert "Working directory: /workspace/project" in event.text
    if result_label is None:
        assert "Result:" not in event.text and "Error:" not in event.text
    else:
        assert result_label in event.text


def test_event_mapping_renders_durable_agent_identifiers() -> None:
    progress = event_to_stream(
        app_event(
            "agent_progress",
            kind="replacement",
            state="leased",
            schedule_id="schedule-1",
            node_id="node-1",
            attempt_id="attempt-2",
            lease_generation=2,
            text="expired attempt replaced",
        )
    )
    assert progress is not None
    assert progress.kind == "subagent"
    assert progress.text == (
        "replacement leased (schedule_id=schedule-1, node_id=node-1, "
        "attempt_id=attempt-2, lease_generation=2) — expired attempt replaced"
    )
    mailbox = event_to_stream(
        app_event("mailbox", state="acknowledged", mailbox_message_id="message-1")
    )
    assert mailbox is not None
    assert mailbox.text == "mailbox acknowledged message-1"


@pytest.mark.asyncio
async def test_readiness_projection_is_sanitized_and_actionable() -> None:
    class Readiness:
        async def doctor(self):
            return {
                "healthy": False,
                "checks": [
                    {
                        "name": "postgresql",
                        "status": "failed",
                        "detail": "postgresql://user:password@localhost/merlin",
                    }
                ],
            }

        async def logs(self, *, limit=100):
            return [{"level": "error", "message": "Bearer secret-token failed"}]

        async def history(self, session_id, *, limit=100):
            return [{"kind": "resume", "summary": "recovered", "token": "secret-token"}]

        async def sessions(self):
            return [
                {
                    "id": "session-1",
                    "title": "Assessment",
                    "updated_at": "2026-08-27T12:00:00Z",
                }
            ]

        async def session_page(self, *, limit, offset, query):
            assert (limit, offset, query) == (10, 20, "refund")
            return {
                "items": [
                    {
                        "id": "session-21",
                        "title": "Refund assessment",
                        "updated_at": "2026-08-28T12:00:00Z",
                    }
                ],
                "total": 21,
                "limit": limit,
                "offset": offset,
                "query": query,
            }

        async def context(self, session_id):
            return {
                "session_id": session_id,
                "projection_head": 2,
                "transcript_head": 3,
                "messages": [{"sequence": 2, "role": "assistant"}],
                "compactions": [],
                "token": "secret-token",
            }

        async def export_config(self):
            return '{"api_key":"secret-token","provider":"ready"}'

    backend = KernelBackend(FakeKernel([]), readiness=Readiness())  # type: ignore[arg-type]

    healthy, checks = await backend.run_doctor()
    assert not healthy
    assert checks[0].detail == "postgresql://localhost/merlin"
    assert (await backend.list_logs())[0].summary == "Bearer [redacted] failed"
    assert "secret-token" not in (await backend.list_history())[0].detail
    assert (await backend.list_resume_sessions())[0].id == "session-1"
    page = await backend.list_resume_session_page(limit=10, offset=20, query="refund")
    assert page.items[0].id == "session-21"
    assert (page.total, page.limit, page.offset, page.query) == (21, 10, 20, "refund")
    context = await backend.conversation_context()
    assert context["projection_head"] == 2
    assert context["token"] == "[redacted]"
    assert "secret-token" not in await backend.export_safe_config()


def test_resume_events_show_recovery_without_replaying_content() -> None:
    recovered = event_to_stream(
        app_event("message", role="assistant", text="sensitive transcript", resumed=True)
    )
    summary = event_to_stream(
        app_event(
            "command",
            command="resume",
            text='{"recovered": 2, "messages": [{}, {}], "schedules": [{}]}',
        )
    )

    assert recovered is None
    assert summary is not None
    assert summary.text == (
        "Resume recovery completed · 2 recovered operations · 2 messages · 1 schedules"
    )


@pytest.mark.asyncio
async def test_mcp_projection_includes_remote_trust_and_health_state() -> None:
    class Mcp:
        async def list_servers(self):
            return (
                {
                    "name": "remote",
                    "status": "running",
                    "tools": ("echo",),
                    "transport": "streamable_http",
                    "disabled": False,
                    "admission": "quarantined",
                    "inventory_digest": "sha256:inventory",
                    "identity": "remote/1.0",
                    "health": "healthy",
                    "reconnect": {"attempts": 2, "delay_seconds": 0.1},
                    "recent_call_audit": ({"at": "now", "tool": "echo", "status": "ok"},),
                },
            )

    backend = KernelBackend(FakeKernel([]), mcp=Mcp())  # type: ignore[arg-type]
    view = (await backend.list_mcp_servers())[0]
    assert view.transport == "streamable_http"
    assert view.admission == "quarantined"
    assert view.inventory_digest == "sha256:inventory"
    assert view.identity == "remote/1.0"
    assert view.health == "healthy"
    assert view.reconnect == "2 attempts · 0.1s delay"
    assert view.recent_audit == ("now · echo · ok",)


class DurableAgents:
    def __init__(self):
        self.activity_schedules = []
        self.mailbox_schedules = []

    async def list_schedules(self, session_id):
        assert session_id == "session-1"
        return (
            ScheduleProjection(
                schedule_id="schedule-1",
                session_id=session_id,
                status="running",
                topology_digest="sha256:graph",
                nodes=(
                    NodeProjection(
                        node_id="node-1",
                        objective="inspect authentication",
                        status="leased",
                        profile_id="authentication_session",
                        technique_ids=("session_fixation", "token_replay"),
                        target_profile_id="target-local",
                        authority_ids=("grant-read", "grant-network"),
                        budget_summary="turns=4 tokens=8000",
                        tool_names=("read_file",),
                        guidance_ids=("auth-playbook",),
                        skill_ids=("reviewer",),
                        provider_route="attacker-primary",
                        dependency_node_ids=("node-0",),
                        attempts_started=2,
                        last_lease_generation=2,
                        lease=LeaseProjection(
                            attempt_id="attempt-2",
                            owner_id="worker-2",
                            generation=2,
                            expires_at_tick=99,
                        ),
                    ),
                ),
                attempts=(
                    AttemptProjection(
                        attempt_id="attempt-1",
                        node_id="node-1",
                        status="failed",
                        owner_id="worker-1",
                        lease_generation=1,
                    ),
                    AttemptProjection(
                        attempt_id="attempt-2",
                        node_id="node-1",
                        status="leased",
                        owner_id="worker-2",
                        lease_generation=2,
                        retry_number=1,
                        replaced_attempt_id="attempt-1",
                    ),
                ),
            ),
        )

    async def list_agents(self, session_id):
        return (
            AgentSummary(
                id="agent-2",
                name="authentication",
                status="leased",
                schedule_id="schedule-1",
                node_id="node-1",
                attempt_id="attempt-2",
                profile_id="authentication_session",
                lease_generation=2,
            ),
        )

    async def list_active_schedules(self):
        return await self.list_schedules("session-1")

    async def list_active_agents(self):
        return await self.list_agents("session-1")

    async def list_active_activity(self):
        return {"node-1": ("recovered after daemon restart",)}

    async def list_schedule_activity(self, schedule_id):
        self.activity_schedules.append(schedule_id)
        return {"node-1": ("recovered after daemon restart",)}

    async def list_schedule_mailbox(self, schedule_id):
        self.mailbox_schedules.append(schedule_id)
        return {
            "node-1": (
                "[pending] lead: inspect refreshed target state",
                "[claimed] lead: preserve the current session",
            )
        }


@pytest.mark.asyncio
async def test_backend_projects_durable_agents_and_schedules() -> None:
    class Campaigns:
        def __init__(self):
            self.schedules = []

        async def list_schedule_directives(self, schedule_id):
            self.schedules.append(schedule_id)
            return {
                "node-1": (
                    "[directive:applied] decision/1/0 · redirect · source=node-1 · "
                    "destination=- · effect=mailbox:message-1 · failure=-",
                    "[directive:failed] decision/2/0 · handoff · source=node-1 · "
                    "destination=node-2 · effect=- · failure=destination unavailable",
                )
            }

        async def recover(self, *, resume_execution=True):
            return None

    durable_agents = DurableAgents()
    campaigns = Campaigns()
    backend = KernelBackend(  # type: ignore[arg-type]
        FakeKernel([]), agents=durable_agents, campaigns=campaigns
    )

    agents = await backend.list_agents()
    schedules = await backend.list_schedules()
    active_schedules = await backend.list_active_schedules()

    assert agents[0].task == "inspect authentication"
    assert agents[0].owner == "worker-2"
    assert agents[0].profile == "authentication_session"
    assert agents[0].authority == "schedule=schedule-1"
    assert agents[0].technique == "session_fixation, token_replay"
    assert agents[0].target_adapter == "target-local"
    assert agents[0].budgets == "turns=4 tokens=8000"
    assert agents[0].activity == (
        "recovered after daemon restart",
        "[directive:applied] decision/1/0 · redirect · source=node-1 · "
        "destination=- · effect=mailbox:message-1 · failure=-",
        "[directive:failed] decision/2/0 · handoff · source=node-1 · "
        "destination=node-2 · effect=- · failure=destination unavailable",
    )
    assert agents[0].mailbox == (
        "[pending] lead: inspect refreshed target state",
        "[claimed] lead: preserve the current session",
    )
    assert durable_agents.activity_schedules == ["schedule-1"]
    assert durable_agents.mailbox_schedules == ["schedule-1"]
    assert campaigns.schedules == ["schedule-1"]
    node = schedules[0].nodes[0]
    assert schedules[0].graph_digest == "sha256:graph"
    assert active_schedules == schedules
    assert node.dependencies == ("node-0",)
    assert node.profile == "authentication_session"
    assert node.technique == "session_fixation, token_replay"
    assert node.target_adapter == "target-local"
    assert node.authority == "grant-read, grant-network"
    assert node.budgets == "turns=4 tokens=8000"
    assert node.tools == ("read_file",)
    assert node.guidance == ("auth-playbook",)
    assert node.skills == ("reviewer",)
    assert node.provider_route == "attacker-primary"
    assert [attempt.id for attempt in node.attempts] == ["attempt-1", "attempt-2"]
    assert node.attempts[1].lease_expiry == "99"
    assert node.attempts[1].retries == "1"


@pytest.mark.asyncio
async def test_backend_agents_use_one_snapshot_read_without_recovery_or_schedule_scans() -> None:
    class SnapshotAgents:
        def __init__(self) -> None:
            self.calls = []

        async def recover(self):
            raise AssertionError("a read must not recover durable execution")

        async def list_schedules(self, session_id):
            raise AssertionError("snapshot path must not scan schedules")

        async def list_swarm_snapshots(self, session_id):
            self.calls.append(session_id)
            return (
                SwarmSnapshot(
                    session_id=session_id,
                    campaign_id="campaign/one",
                    scope_provenance=IdentityProvenance.DURABLE,
                    specialists=(
                        SpecialistSnapshot(
                            specialist_instance_id="specialist/first",
                            identity_provenance=IdentityProvenance.DURABLE,
                            display_name="Prompt injection specialist",
                            lifecycle=NodeLifecycle.RUNNING,
                            assignments=(
                                AssignmentSnapshot(
                                    assignment_id="task/first",
                                    schedule_id="schedule/first",
                                    lifecycle=NodeLifecycle.RUNNING,
                                    candidate=CandidateAxes(
                                        target_id="target/one",
                                        target_generation="generation/one",
                                        scenario_fingerprint="sha256:scenario",
                                        technique_id="technique/one",
                                        controller_id="pair",
                                        payload_family="rot13",
                                        requested_language="lzh",
                                        mutation_id="mutation/rot13",
                                    ),
                                    execution_stage="direct",
                                    provider_model="deepseek-v4-flash",
                                    current_attempt=CurrentAttempt(
                                        attempt_id="attempt/current",
                                        lifecycle=AttemptLifecycle.RUNNING,
                                    ),
                                    candidate_evaluation=CandidateEvaluationState(
                                        state=CandidateEvaluation.EVALUATION_UNCERTAIN,
                                        evaluation_confidence=0.4,
                                        evaluation_reasons=("validator disagreement",),
                                    ),
                                    successful_result=SuccessfulResultSummary(
                                        result_id="attempt/previous",
                                        summary="previous target reply",
                                    ),
                                ),
                            ),
                        ),
                        SpecialistSnapshot(
                            specialist_instance_id="specialist/second",
                            identity_provenance=IdentityProvenance.DURABLE,
                            display_name="Prompt injection specialist",
                            lifecycle=NodeLifecycle.QUEUED,
                            assignments=(
                                AssignmentSnapshot(
                                    assignment_id="task/second",
                                    schedule_id="schedule/second",
                                    lifecycle=NodeLifecycle.QUEUED,
                                    candidate=CandidateAxes(
                                        target_id="target/one",
                                        target_generation="generation/one",
                                        scenario_fingerprint="sha256:scenario",
                                        technique_id="technique/two",
                                        controller_id="goat",
                                        payload_family="baseline",
                                        requested_language="am",
                                        mutation_id="none",
                                    ),
                                    execution_stage="execute",
                                    provider_model="deepseek-v4-flash",
                                    current_attempt=None,
                                    candidate_evaluation=CandidateEvaluationState(),
                                    successful_result=None,
                                ),
                            ),
                        ),
                    ),
                ),
            )

    agents = SnapshotAgents()
    backend = KernelBackend(FakeKernel([]), agents=agents)  # type: ignore[arg-type]

    views = await backend.list_agents()

    assert agents.calls == ["session-1"]
    assert [item.specialist_instance_id for item in views] == [
        "specialist/first",
        "specialist/second",
    ]
    assert views[0].profile == views[1].profile == "Prompt injection specialist"
    assert views[0].assignment_id == "task/first"
    assert views[0].evaluation_state == "evaluation_uncertain"
    assert views[0].result == "previous target reply"
    assert views[0].provider == views[1].provider == "deepseek-v4-flash"
    assert views[1].requested_language == "am"


@pytest.mark.asyncio
async def test_background_recovery_renews_campaign_ownership_but_recovers_agents_once() -> None:
    calls = []

    class RecoverableAgents:
        async def recover(self, *, resume_execution=True):
            calls.append(("agents", resume_execution))

    class RecoverableCampaigns:
        async def recover(self, *, resume_execution=True):
            calls.append(("campaigns", resume_execution))

    backend = KernelBackend(  # type: ignore[arg-type]
        FakeKernel([]),
        agents=RecoverableAgents(),
        campaigns=RecoverableCampaigns(),
    )

    await backend.recover_background()
    await backend.recover_background()

    assert calls == [
        ("campaigns", True),
        ("agents", False),
        ("campaigns", True),
    ]


class Findings:
    async def list_findings(self, session_id):
        assert session_id == "session-1"
        return (
            Finding(
                id="finding-7",
                title="Cross-tenant disclosure",
                severity="high",
                summary="A tenant boundary was crossed.",
                reproduction="Send tenant B identifier.",
                remediation="Bind records to authenticated tenant.",
                creator="agent-auth",
                evidence_references=["run-7/message-12"],
                status="accepted",
            ),
        )


@pytest.mark.asyncio
async def test_backend_projects_real_findings_and_retains_generated_report_history() -> None:
    kernel = FakeKernel(
        [
            app_event(
                "report",
                state="completed",
                format="markdown",
                output="reports/session-1.md",
                finding_count=1,
            )
        ]
    )
    backend = KernelBackend(kernel, findings=Findings())  # type: ignore[arg-type]

    findings = await backend.list_findings()
    events = [event async for event in backend.stream("/report run --format markdown --output x")]

    assert findings[0].disposition == "accepted"
    assert findings[0].evidence == ("run-7/message-12",)
    assert events[0].text == "Report completed → reports/session-1.md · 1 accepted findings"
    assert (await backend.list_reports())[0].output == "reports/session-1.md"


@pytest.mark.asyncio
async def test_durable_reports_are_daemon_wide_across_console_sessions() -> None:
    class Campaigns:
        async def list_all(self):
            return ({"campaign_id": "campaign/historical"},)

        async def list(self, _session_id):
            raise AssertionError("durable reports must not be scoped to the current console")

    class Reports:
        def list(self, campaign_id):
            assert campaign_id == "campaign/historical"
            return [
                SimpleNamespace(
                    campaign_id=campaign_id,
                    revision_id=f"{campaign_id}:r1",
                    metadata={"export_path": "/workspace/target/reports/campaign-historical.html"},
                    stop_reason="saturated",
                    cleanup_status="complete",
                    findings=(1,),
                    exchanges=(1, 2),
                )
            ]

    kernel = FakeKernel([])
    kernel.session_id = "new-console-session"
    backend = KernelBackend(  # type: ignore[arg-type]
        kernel,
        campaigns=Campaigns(),
        report_repository=Reports(),
    )

    reports = await backend.list_reports()

    assert [(item.run, item.finding_count, item.exchange_count) for item in reports] == [
        ("campaign/historical", 1, 2)
    ]
    assert reports[0].output == "/workspace/target/reports/campaign-historical.html"


@pytest.mark.asyncio
@pytest.mark.parametrize("has_campaign", [False, True])
async def test_session_report_lookup_does_not_scan_historical_campaigns(has_campaign) -> None:
    class Campaigns:
        async def list_all(self):
            raise AssertionError("Chat must not enumerate historical campaigns")

        async def list(self, session_id, *, limit):
            assert session_id == "new-console-session"
            assert limit is None
            return ({"campaign_id": "campaign/current"},) if has_campaign else ()

    class Reports:
        def list(self, campaign_id):
            assert campaign_id == "campaign/current"
            return [
                SimpleNamespace(
                    campaign_id=campaign_id,
                    revision_id="r1",
                    metadata={},
                    stop_reason="saturated",
                    cleanup_status="complete",
                    findings=(),
                    exchanges=(),
                )
            ]

    kernel = FakeKernel([])
    kernel.session_id = "new-console-session"
    backend = KernelBackend(kernel, campaigns=Campaigns(), report_repository=Reports())
    reports = await backend.list_reports(session_only=True)
    assert [report.run for report in reports] == (["campaign/current"] if has_campaign else [])


def test_finding_disposition_and_report_failure_events_are_clear() -> None:
    accepted = event_to_stream(app_event("finding", action="accept", finding={"id": "finding-7"}))
    rejected = event_to_stream(app_event("finding", action="reject", finding={"id": "finding-8"}))
    failure = event_to_stream(app_event("report", state="failed", text="disk full"))

    assert accepted is not None and accepted.text == "Finding finding-7 accepted"
    assert rejected is not None and rejected.text == "Finding finding-8 rejected"
    assert failure is not None and failure.kind == "error"
    assert failure.text == "Report failed: disk full"


class Permissions:
    mode = "prompt"

    async def get_mode(self):
        return self.mode

    async def set_mode(self, mode, *, session_id):
        assert session_id == "session-1"
        self.mode = mode
        return mode

    async def list_permissions(self, session_id):
        assert session_id == "session-1"
        return [
            {
                "tool": "target_chat",
                "effect": "target_network",
                "mode": "prompt",
                "verdict": "allow",
                "decision": "allow_session",
                "reason": "operator decision",
                "scope": "sha256:scope",
            },
            {
                "tool": "read_file",
                "effect": "workspace_read",
                "mode": "auto",
                "verdict": "allow",
                "decision": None,
                "reason": "auto mode default",
                "scope": "sha256:read",
            },
        ]


@pytest.mark.asyncio
async def test_backend_projects_permission_service_mode_rules_and_history() -> None:
    service = Permissions()
    backend = KernelBackend(FakeKernel([]), permissions=service)  # type: ignore[arg-type]

    assert await backend.get_permission_mode() == "prompt"
    assert await backend.set_permission_mode("yolo") == "yolo"
    rules = await backend.list_permission_rules()
    history = await backend.list_permission_history()

    assert rules[0].decision == "allow_session"
    assert rules[0].scope == "sha256:scope"
    assert [row.tool for row in history] == ["target_chat", "read_file"]


@pytest.mark.asyncio
async def test_backend_includes_exact_command_grants_in_permission_rules() -> None:
    class CommandPermissions:
        async def list_permissions(self, session_id):
            return [
                {
                    "tool": "shell_command",
                    "effect": "subprocess",
                    "mode": "prompt",
                    "verdict": "allow",
                    "decision": "allow_command_session",
                    "reason": "operator exact command session decision",
                    "scope": "sha256:exact-command",
                },
            ]

    backend = KernelBackend(FakeKernel([]), permissions=CommandPermissions())  # type: ignore[arg-type]

    rules = await backend.list_permission_rules()
    history = await backend.list_permission_history()

    assert len(rules) == 1
    assert rules[0].decision == "allow_command_session"
    assert rules[0].scope == "sha256:exact-command"
    assert history[0].decision == "allow_command_session"


@pytest.mark.asyncio
async def test_backend_hot_reload_applies_reload_safe_settings_and_reports_drift() -> None:
    settings = KernelSettings(
        Config(
            values={
                "permission_mode": "prompt",
                "provider": {"base_url": "https://old.test/v1", "model": "old"},
            },
            sources={"permission_mode": "file", "provider.model": "file"},
        ),
        "old",
    )
    permissions = Permissions()
    backend = KernelBackend(  # type: ignore[arg-type]
        FakeKernel([]), settings=settings, permissions=permissions
    )

    status = await backend.reload_configuration(
        Config(
            values={
                "permission_mode": "yolo",
                "provider": {"base_url": "https://new.test/v1", "model": "new"},
            },
            sources={"permission_mode": "file", "provider.model": "file"},
        )
    )

    assert await permissions.get_mode() == "yolo"
    assert status == {
        "applied": ("permission_mode", "provider.model"),
        "restart_required": ("provider.base_url",),
    }
    assert settings.config.values["permission_mode"] == "yolo"
    # A saved model change reaches the running daemon: the next turn's request
    # re-resolves the model (and its per-route budget) without a restart.
    assert settings.config.values["provider"]["model"] == "new"
    assert await settings.get_model() == "new"
    runtime = await backend.get_runtime_status()
    assert runtime["config_reload"] == status

    backend.configuration_reload_failed(ValueError("api_key=must-not-leak"))
    assert backend.config_reload_status == {
        "applied": (),
        "restart_required": ("provider.base_url",),
        "error": "ValueError",
    }


@pytest.mark.asyncio
async def test_backend_hot_reload_applies_role_route_without_overriding_active_route() -> None:
    settings = KernelSettings(
        Config(
            values={
                "provider": {
                    "model": "base",
                    "role_models": {"campaign-lead": "lead-old"},
                }
            },
        ),
        "base",
    )
    backend = KernelBackend(FakeKernel([]), settings=settings)  # type: ignore[arg-type]
    assert settings.use_role_model("campaign-lead", "lead") == "lead-old"

    status = await backend.reload_configuration(
        Config(values={"provider": {"model": "base", "role_models": {"campaign-lead": "lead-new"}}})
    )

    assert status["applied"] == ("provider.role_models",)
    assert settings.config.provider_role_models == {"campaign-lead": "lead-new"}
    # The active session route is not clobbered mid-turn; the next campaign
    # turn re-resolves from the reloaded routes.
    assert await settings.get_model() == "lead-old"
    assert settings.use_role_model("campaign-lead", "lead") == "lead-new"


class Memory:
    async def list_memory(self, status):
        assert status == "quarantined"
        return (
            {
                "bundle_id": "bundle/import-1",
                "record_id": "lesson/pending",
                "kind": "reviewed_skill",
                "status": "quarantined",
                "source": "merlin://external/team",
                "reviewed_by": None,
                "expires_at": None,
                "applicability": {"target_ids": ["target-a"], "tags": ["auth"]},
                "digest": "sha256:record",
                "conflicts": ["lesson/existing"],
                "turn_uses": [],
            },
        )


@pytest.mark.asyncio
async def test_backend_projects_portable_memory_service_view() -> None:
    backend = KernelBackend(FakeKernel([]), memory=Memory())  # type: ignore[arg-type]

    records = await backend.list_memory_records("quarantined")

    assert records[0].status == "quarantined"
    assert records[0].applicability == "target_ids=target-a; tags=auth"
    assert records[0].conflicts == ("lesson/existing",)


def test_memory_event_makes_agent_turn_use_visible() -> None:
    event = event_to_stream(
        app_event(
            "memory",
            action="used_in_turn",
            record_ids=["lesson/auth-1"],
            turn_id="attempt-2",
        )
    )
    assert event is not None
    assert event.text == "Memory used_in_turn · lesson/auth-1 · turn attempt-2"


def test_prepared_review_uses_structured_event_when_tool_json_is_truncated() -> None:
    import json

    prepared = {
        "campaign_id": "campaign/large",
        "manifest_digest": "sealed",
        "review": {"budget": {"turns": None}},
    }
    event = event_to_stream(
        app_event(
            "tool",
            name="assessment_prepare",
            state="completed",
            text='{"review": {"cross_campaign_learning": {"decisions": [',
            campaign_review=prepared,
        )
    )
    assert event.kind == "campaign_review"
    assert json.loads(event.text) == prepared
    failed = event_to_stream(
        app_event(
            "tool",
            name="assessment_prepare",
            state="failed",
            text="preparation failed",
            campaign_review=prepared,
        )
    )
    assert failed.kind == "tool"


@pytest.mark.asyncio
async def test_saved_review_restores_structured_event_with_exact_digest() -> None:
    saved = {
        "campaign_id": "campaign/large",
        "manifest_digest": "sealed",
        "review": {"budget": {"turns": None}},
    }

    class Store:
        async def load_messages(self, session_id):
            return [SimpleNamespace(role="tool", content='{"review":')]

        async def list_events(self, session_id):
            return [
                {
                    "type": "tool",
                    "payload": {
                        "name": "assessment_prepare",
                        "state": "completed",
                        "campaign_review": saved,
                    },
                },
                {
                    "type": "tool",
                    "payload": {
                        "name": "assessment_prepare",
                        "state": "completed",
                        "campaign_review": dict(saved, manifest_digest="obsolete"),
                    },
                },
                {
                    "type": "tool",
                    "payload": {
                        "name": "assessment_prepare",
                        "state": "failed",
                        "campaign_review": dict(saved, review={"wrong": True}),
                    },
                },
            ]

    kernel = FakeKernel([])
    kernel._store = Store()
    backend = KernelBackend(kernel)
    assert await backend._saved_campaign_review("campaign/large", "sealed") == saved
    assert await backend._saved_campaign_review("campaign/other", "sealed") is None
    assert await backend._saved_campaign_review("campaign/large", "unknown") is None


class Promotions:
    async def list_candidates(self):
        return ({"id": "candidate-1"},)

    async def show_candidate(self, candidate_id):
        assert candidate_id == "candidate-1"
        return {
            "id": candidate_id,
            "kind": "skill",
            "version": "2",
            "status": "evaluated",
            "baseline_version": "1",
            "baseline_digest": "sha256:baseline",
            "content_digest": "sha256:candidate",
            "exact_diff": "- old\n+ new",
            "proposed_by": "agent:worker",
            "permanently_promotable": True,
            "graph_local_composition": False,
            "evaluations": [
                {
                    "gates": [
                        {
                            "name": "authority",
                            "passed": False,
                            "observed": 1,
                            "threshold": 0,
                        }
                    ]
                }
            ],
        }

    async def history(self, kind):
        assert kind is None
        return (
            {
                "candidate_id": "candidate-1",
                "previous_candidate_id": "candidate-0",
                "action": "activated",
                "operator_id": "operator:alice",
                "activated_at": "2026-08-27T12:00:00Z",
            },
        )


@pytest.mark.asyncio
async def test_backend_projects_promotion_diff_evaluation_failure_and_history() -> None:
    backend = KernelBackend(FakeKernel([]), promotions=Promotions())  # type: ignore[arg-type]

    candidates = await backend.list_promotion_candidates()
    history = await backend.list_promotion_history()

    assert candidates[0].exact_diff == "- old\n+ new"
    assert candidates[0].gates[0].detail == "observed 1 · threshold 0"
    assert candidates[0].evaluation_failure == "Failed gates: authority"
    assert history[0].previous_candidate_id == "candidate-0"


@pytest.mark.asyncio
async def test_model_change_updates_live_setting_and_persists(tmp_path) -> None:
    paths = ConfigPaths(tmp_path / "merlin")
    configuration = ProviderConfiguration(paths)
    configuration.save("https://example.test/v1", "old-model")
    settings = KernelSettings(
        Config(values={"provider": {"base_url": "https://example.test/v1"}}),
        "old-model",
        configuration,
    )
    backend = KernelBackend(FakeKernel([]), settings=settings, configuration=configuration)  # type: ignore[arg-type]

    await backend.set_model("new-model")

    assert await settings.get_model() == "new-model"
    assert load_config(paths, environ={}).values["provider"]["model"] == "new-model"


@pytest.mark.asyncio
async def test_browser_projection_and_takeover_adapter() -> None:
    class Browser:
        async def list_contexts(self, session_id):
            assert session_id == "session-1"
            return (
                {
                    "context_id": "context-1",
                    "mode": "fresh",
                    "target_id": "target-1",
                    "worker_id": "worker-1",
                    "technique_id": "injection",
                    "state": "active",
                    "allowed_origins": ("https://target.test",),
                    "latest_action": "navigate",
                    "latest_observation": "login loaded",
                    "blockers": ("needs auth",),
                    "evidence": ("evidence://action-1",),
                    "cleanup_state": "pending",
                },
            )

        async def list_tabs(self, session_id, context_id):
            return ({"tab_ref": "tab-1", "url": "https://target.test", "state": "open"},)

        async def takeover(self, session_id, context_id):
            context = (await self.list_contexts(session_id))[0]
            return {**context, "worker_id": "operator", "state": "operator takeover"}

    kernel = FakeKernel([])
    kernel.session_id = "session-1"
    backend = KernelBackend(kernel, browser=Browser())  # type: ignore[arg-type]

    context = (await backend.list_browser_contexts())[0]
    assert context.isolation == "fresh"
    assert context.tabs[0].ref == "tab-1"
    assert context.blockers == ("needs auth",)
    kernel.session_id = "different-console-session"
    historical = (await backend.list_browser_contexts("session-1"))[0]
    assert historical.id == "context-1"
    kernel.session_id = "session-1"
    taken = await backend.takeover_browser_context("context-1")
    assert taken.owner == "operator"
    assert taken.state == "operator takeover"


@pytest.mark.asyncio
async def test_campaign_list_is_daemon_wide_across_console_sessions() -> None:
    class Campaigns:
        async def list_all(self):
            return (
                {
                    "campaign_id": "campaign/historical",
                    "session_id": "session-historical",
                    "objective": "Assess target",
                    "profile": "recommended",
                    "status": "completed",
                    "manifest_digest": "sha256:abc",
                },
                {
                    "campaign_id": "campaign/current",
                    "session_id": "new-console-session",
                    "objective": "Assess current target",
                    "profile": "recommended",
                    "status": "executing",
                    "manifest_digest": "sha256:def",
                    "stop_reason": "infrastructure_blocked_after_retry_policy",
                },
            )

        async def list(self, session_id, *, limit):
            assert session_id == "new-console-session"
            assert limit is None
            return tuple(row for row in await self.list_all() if row["session_id"] == session_id)

    kernel = FakeKernel([])
    kernel.session_id = "new-console-session"
    backend = KernelBackend(kernel, campaigns=Campaigns())  # type: ignore[arg-type]

    campaigns = await backend.list_campaigns()

    assert [campaign.id for campaign in campaigns] == ["campaign/current"]
    assert campaigns[0].stop_reason == "infrastructure_blocked_after_retry_policy"


@pytest.mark.asyncio
async def test_campaign_saturation_decision_uses_durable_daemon_ownership() -> None:
    calls = []

    class Campaigns:
        async def list_all(self):
            return (
                {
                    "campaign_id": "campaign/old",
                    "session_id": "new-console-session",
                },
            )

        async def decide_saturation(
            self,
            session_id,
            campaign_id,
            decision,
            *,
            additional_waves=None,
        ):
            calls.append((session_id, campaign_id, decision, additional_waves))
            return {"decision": decision}

    kernel = FakeKernel([])
    kernel.session_id = "new-console-session"
    backend = KernelBackend(kernel, campaigns=Campaigns())  # type: ignore[arg-type]

    result = await backend.decide_campaign_saturation("campaign/old", "continue")

    assert result == {"decision": "continue"}
    assert calls == [("new-console-session", "campaign/old", "continue", None)]


@pytest.mark.asyncio
async def test_snapshot_views_keep_objective_live_activity_and_failure_reason() -> None:
    from merlin_agents.durable_service import _project_durable_swarm_rows

    snapshots = _project_durable_swarm_rows(
        [
            {
                "session_id": "session-1",
                "campaign_id": "campaign/one",
                "schedule_id": "schedule/one",
                "schedule_status": "running",
                "schedule_updated_at": datetime(2026, 10, 28, 11, tzinfo=UTC),
                "campaign_wave": 2,
                "assignment": {
                    "id": "probe/memory",
                    "objective": "Test whether target memory persists",
                },
                "node_id": "worker:probe/memory",
                "node_state": "failed",
                "current_attempt_id": "attempt/one",
                "current_attempt_state": "failed",
                "current_attempt_error": "Target timed out while reading the second response",
                "current_attempt_failure_kind": "configuration",
                "current_infrastructure_failure_category": "adaptive_strategy_unavailable",
                "recent_output": [
                    {"role": "assistant", "content": "Testing recall across turns"},
                    {"role": "tool", "content": "Target replied: I do not remember"},
                ],
                "current_result_content": "The target did not retain the supplied instruction",
                "recent_activity": [
                    {
                        "type": "checkpointed",
                        "payload": {
                            "phase": "waiting for target response",
                            "action_count": 3,
                        },
                    }
                ],
            }
        ]
    )
    backend = KernelBackend(FakeKernel([]))
    (view,) = await backend._snapshot_agent_views(snapshots)
    assert view.task == "Test whether target memory persists"
    assert "waiting for target response" in view.activity[-1]
    assert "3 actions" in view.activity[-1]
    assert view.result == "Target timed out while reading the second response"
    assert view.failure_kind == "configuration"
    assert view.infrastructure_failure_category == "adaptive_strategy_unavailable"
    assert view.schedule_updated_at == datetime(2026, 10, 28, 11, tzinfo=UTC)
    assert view.campaign_wave == 2
    assert "assistant: Testing recall across turns" in view.output
    assert "tool: Target replied: I do not remember" in view.output
    assert view.output[-1] == "Result: The target did not retain the supplied instruction"


@pytest.mark.asyncio
async def test_expanded_agent_transcript_uses_current_session_scope() -> None:
    class Agents:
        calls = []

        async def get_assignment_transcript(self, session_id, assignment_id):
            self.calls.append((session_id, assignment_id))
            return ("assistant: retained output", "tool: target response")

    agents = Agents()
    backend = KernelBackend(FakeKernel([]), agents=agents)
    assert await backend.get_agent_transcript("assignment-7") == (
        "assistant: retained output",
        "tool: target response",
    )
    assert agents.calls == [("session-1", "assignment-7")]


@pytest.mark.asyncio
@pytest.mark.parametrize("format,suffix", [("markdown", ".md"), ("json", ".json"), ("csv", ".zip")])
async def test_export_saved_campaign_uses_selected_revision_not_current_session(
    tmp_path,
    monkeypatch,
    format,
    suffix,
):
    from merlin_findings.projection import DurableReportService

    html = tmp_path / "historical.html"
    html.write_text("Saved HTML")
    revision = SimpleNamespace(
        revision_id="historical:r2",
        metadata={"export_path": str(html)},
        findings=(1, 2),
        stop_reason="failed",
        cleanup_status="completed",
        report_state="incomplete",
    )
    calls = []

    class Reports:
        def list(self, campaign_id):
            calls.append(campaign_id)
            return [revision] if campaign_id == "campaign/historical" else []

    exported = []

    def export(self, revision_id, selected_format):
        exported.append((revision_id, selected_format))
        return b"CSV bundle bytes" if selected_format == "csv" else "Saved historical evidence"

    monkeypatch.setattr(DurableReportService, "export", export)
    kernel = FakeKernel([])
    kernel.session_id = "unrelated-current-session"
    backend = KernelBackend(kernel, report_repository=Reports())
    result = await backend.export_campaign_report("campaign/historical", str(html), format)
    assert calls == ["campaign/historical"]
    assert exported == [("historical:r2", format)]
    assert result.run == "campaign/historical"
    assert result.output == str(html.with_suffix(suffix))
    assert html.with_suffix(suffix).is_file()
    assert html.read_text() == "Saved HTML"
    assert result in await backend.list_reports()
    with pytest.raises(ValueError, match="no longer exists"):
        await backend.export_campaign_report("campaign/other", str(html), format)


@pytest.mark.asyncio
async def test_resume_restores_exact_prepared_review_without_execution() -> None:
    import json

    saved = {
        "campaign_id": "campaign/saved",
        "manifest_digest": "sealed",
        "objective": "Saved target assessment",
        "review": {"budget": {"turns": 10}},
        "approval_required": True,
    }

    class Store:
        async def load_messages(self, session_id):
            assert session_id == "session-1"
            return [
                SimpleNamespace(
                    role="tool",
                    content=json.dumps(saved) + '\n[merlin-automatic-recon]{"state":"prepared"}',
                ),
                SimpleNamespace(
                    role="tool", content=json.dumps(dict(saved, manifest_digest="obsolete"))
                ),
            ]

    class Campaigns:
        async def recover(self, *, resume_execution=True):
            pass

        async def list_all(self):
            return [dict(saved, session_id="session-1", status="prepared", profile="recommended")]

        async def list(self, session_id, *, limit):
            assert limit is None
            return await self.list_all()

        async def execute(self, *args):
            raise AssertionError("Resume must not execute an unapproved plan")

    kernel = FakeKernel([])
    kernel._store = Store()
    backend = KernelBackend(kernel, campaigns=Campaigns())
    events = [event async for event in backend.stream("/resume session-1")]
    reviews = [json.loads(event.text) for event in events if event.kind == "campaign_review"]
    assert reviews == [saved]
    assert any("Execution has not started" in event.text for event in events)


@pytest.mark.parametrize(
    "phrase",
    [
        "redpen scan",
        "red pen scan",
        "red-team scan",
        "red team scan",
        "merlin scan",
    ],
)
def test_scan_spellings_require_campaign_profile(phrase):
    from merlin_tui.kernel_backend import _needs_campaign_profile

    assert _needs_campaign_profile(f"Run a {phrase} on https://plunder.me, refund trap L2")
    assert not _needs_campaign_profile(
        f"Run a {phrase} on https://plunder.me, campaign profile: quickscan"
    )
    assert not _needs_campaign_profile("/campaign resume campaign/saved")


@pytest.mark.asyncio
@pytest.mark.parametrize("profile", ["recommended", "exhaustive", "exploration", "quickscan"])
@pytest.mark.parametrize("retry_preflight", [False, True])
async def test_profile_and_autonomous_choice_reaches_kernel_once(profile, retry_preflight):
    kernel = FakeKernel([])
    checks = []

    async def preflight(selected):
        checks.append(selected)
        return (not retry_preflight or len(checks) > 1, "route status")

    backend = KernelBackend(kernel, route_preflight=preflight)
    objective = "Run a merlin scan on https://target.test"
    first = [event async for event in backend.stream(objective)]
    assert "Choose the campaign profile" in first[0].text
    answer = (
        f"{profile}\nThe operator explicitly selected specialist composition: autonomous "
        "(orthogonal to the coverage profile); pass specialist_composition='autonomous' "
        "to assessment_prepare and do not ask again."
    )
    selected = [event async for event in backend.stream(answer)]
    if retry_preflight:
        assert kernel.submissions == []
        selected += [event async for event in backend.stream("2")]
    assert not any("Choose the campaign profile" in event.text for event in selected)
    assert len(kernel.submissions) == 1
    submitted = kernel.submissions[0]
    assert submitted.startswith(objective + "\n")
    assert f"campaign profile: {profile};" in submitted
    assert "specialist_composition='autonomous'" in submitted
    assert all(check == profile for check in checks)


@pytest.mark.asyncio
async def test_campaign_selection_retains_target_while_waiting_for_active_turn():
    kernel = FakeKernel([])
    backend = KernelBackend(kernel)
    objective = "Run a merlin scan on https://target.test"
    _ = [event async for event in backend.stream(objective)]
    kernel.turn_active = True

    async def wait_for_turn():
        assert backend._pending_campaign_objective == objective
        assert not kernel.submissions
        kernel.turn_active = False

    kernel.wait_for_turn = wait_for_turn
    events = [event async for event in backend.stream("quickscan")]
    assert any("Waiting for the active response" in event.text for event in events)
    assert len(kernel.submissions) == 1
    assert kernel.submissions[0].startswith(objective + "\n")
    assert backend._pending_campaign_objective is None


@pytest.mark.asyncio
async def test_preflight_cancel_clears_retained_campaign_target():
    async def preflight(profile):
        return False, "route unavailable"

    kernel = FakeKernel([])
    backend = KernelBackend(kernel, route_preflight=preflight)
    _ = [event async for event in backend.stream("Run a merlin scan on https://target.test")]
    _ = [event async for event in backend.stream("quickscan")]
    _ = [event async for event in backend.stream("cancel")]
    assert backend._pending_campaign_objective is None
    assert backend._pending_campaign_request_id is None
    assert backend._pending_route_preflight is None
    assert not kernel.submissions


def _sealed_review_row():
    from dataclasses import asdict

    from merlin_agents import (
        AssignmentBudget,
        AssignmentContext,
        CampaignManifest,
        CampaignProfile,
        ReviewedTargetAdapter,
    )
    from merlin_agents.campaign_service import _json_value, _reviewed_preparation_digest

    budget = AssignmentBudget(
        max_turns=12,
        max_tokens=120_000,
        max_seconds=900,
        max_cost_usd=3.0,
    )
    manifest = CampaignManifest.create(
        id="campaign/recovery",
        session_id="session-recovery",
        objective="Saved objective",
        profile=CampaignProfile.QUICKSCAN,
        target=ReviewedTargetAdapter(
            "target", "http_api", "https://target.test", credential_handle="secret-ref"
        ),
        provider_id="provider",
        provider_model="model",
        provider_effort="default",
        authority_ids=("target:target",),
        catalogue=(),
        skill_documents=(),
        tool_ids=("target_request",),
        budget=budget,
        specialist_composition="autonomous",
    )
    context = AssignmentContext(
        target_profile_id="target",
        techniques=(),
        tool_ids=manifest.tool_ids,
        skill_ids=(),
        provider_id="provider",
        authority_ids=manifest.authority_ids,
        budget=budget,
        input_schema="secret input schema",
        output_schema="output",
    )
    return {
        "id": manifest.id,
        "session_id": manifest.session_id,
        "status": "prepared",
        "manifest_digest": _reviewed_preparation_digest(manifest, context),
        "manifest": {
            "manifest": _json_value(asdict(manifest)),
            "assignment_context": _json_value(asdict(context)),
        },
    }


@pytest.mark.asyncio
async def test_saved_review_recovers_sealed_plan_after_truncated_transcript():
    import json

    row = _sealed_review_row()
    kernel = FakeKernel([])
    kernel.session_id = row["session_id"]

    class Store:
        async def load_messages(self, session_id):
            return [SimpleNamespace(role="tool", content='{"review":')]

    kernel._store = Store()
    campaigns = SimpleNamespace(_store=SimpleNamespace(get=lambda campaign_id: row))
    backend = KernelBackend(kernel, campaigns=campaigns)
    restored = await backend._saved_campaign_review(row["id"], row["manifest_digest"])
    assert restored["review"]["recovered_fields"]["Agent composition"] == "autonomous"
    assert restored["review"]["budget"] == {
        "turns": 12,
        "tokens": 120_000,
        "seconds": 900,
        "cost_usd": 3.0,
    }
    assert restored["approval_required"] is True
    assert restored["manifest_digest"] == row["manifest_digest"]
    assert "secret-ref" not in json.dumps(restored)
    assert "secret input schema" not in json.dumps(restored)
    assert kernel.submissions == []


@pytest.mark.parametrize(
    "mutation", ["session", "campaign", "status", "digest", "manifest", "context"]
)
def test_saved_review_rejects_unbound_or_modified_plan(mutation):
    from merlin_agents.campaign_review import recover_campaign_review

    row = _sealed_review_row()
    digest = row["manifest_digest"]
    if mutation == "session":
        row["session_id"] = "other"
    elif mutation == "campaign":
        row["id"] = "other"
    elif mutation == "status":
        row["status"] = "executing"
    elif mutation == "digest":
        row["manifest_digest"] = "sha256:wrong"
    elif mutation == "manifest":
        row["manifest"]["manifest"]["objective"] = "Changed objective"
    else:
        row["manifest"]["assignment_context"]["tool_ids"] = ["unapproved_tool"]
    with pytest.raises(ValueError):
        recover_campaign_review(
            row,
            session_id="session-recovery",
            campaign_id="campaign/recovery",
            manifest_digest=digest,
        )


@pytest.mark.asyncio
async def test_campaign_inventory_projects_durable_wave_without_full_status():
    from merlin_agents.campaign_service import CampaignService

    class Store:
        def list_waves(self, campaign_id):
            assert campaign_id == "campaign/live"
            return [
                {"wave": 0, "kind": "capability", "status": "completed"},
                {"wave": 1, "kind": "attack", "status": "running"},
            ]

        def get(self, campaign_id):
            assert campaign_id == "campaign/live"
            row = _sealed_review_row()
            row["id"] = campaign_id
            return row

    service = object.__new__(CampaignService)
    service._store = Store()

    class Agents:
        async def target_progress(self, campaign_id, target_id):
            assert campaign_id == "campaign/live"
            assert target_id == "target"
            return {
                "schema": "merlin.target-progress.v1",
                "status": "unknown",
                "current_state": (),
            }

    service._agents = Agents()

    class Campaigns:
        async def list_all(self):
            return [
                {
                    "campaign_id": "campaign/live",
                    "session_id": "session-1",
                    "objective": "Refund policy",
                    "profile": "quickscan",
                    "status": "executing",
                    "manifest_digest": "digest",
                }
            ]

        async def wave_progress(self, campaign_id):
            progress = dict(await service.wave_progress(campaign_id))
            progress["review_progress"] = {
                "review_kind": "wave_results",
                "blocking": True,
                "elapsed_seconds": 42,
                "last_activity": "Checking state transitions",
                "waiting_for": "Next-wave selection",
            }
            return progress

        async def list(self, session_id, *, limit):
            assert session_id == "session-1"
            assert limit is None
            return await self.list_all()

    backend = KernelBackend(FakeKernel([]), campaigns=Campaigns())
    campaigns = await backend.list_campaigns()
    assert campaigns[0].wave_number == 2
    assert campaigns[0].target_progress["status"] == "unknown"
    assert campaigns[0].wave_kind == "attack"
    assert campaigns[0].wave_status == "running"
    assert campaigns[0].review_progress == {
        "review_kind": "wave_results",
        "blocking": True,
        "elapsed_seconds": 42,
        "last_activity": "Checking state transitions",
        "waiting_for": "Next-wave selection",
    }


@pytest.mark.asyncio
async def test_startup_and_new_session_keep_authorized_campaign_work_running() -> None:
    calls = []

    class StartupKernel(FakeKernel):
        async def start(self):
            self.session_id = "startup-session"

        async def submit(self, text):
            if text == "/new":
                self.session_id = "new-session"
            await super().submit(text)

    class Campaigns:
        status = "executing"

        async def recover(self, *, resume_execution=True):
            calls.append(("campaigns", resume_execution))
            assert resume_execution

    class Agents:
        async def recover(self, *, resume_execution=True):
            assert not resume_execution
            calls.append(("agents", False))

        async def list_schedules(self, session_id):
            return ()

    kernel = StartupKernel([])
    kernel.session_id = None
    campaigns = Campaigns()
    backend = KernelBackend(kernel, campaigns=campaigns, agents=Agents())
    await backend.recover_background()
    assert campaigns.status == "executing"
    await backend.list_schedules()
    assert [event async for event in backend.stream("/new")] == []
    await backend.recover_background()
    assert kernel.session_id == "new-session"
    assert campaigns.status == "executing"
    assert calls == [("campaigns", True), ("agents", False), ("campaigns", True)]


async def test_conversation_history_is_paged_sanitized_and_session_bound():
    from unittest.mock import AsyncMock

    kernel = FakeKernel([])
    loader = AsyncMock(
        return_value={
            "session_id": "session-1",
            "messages": [{"content": "Bearer private-key"}],
            "before": 12,
            "has_more": True,
        }
    )
    kernel._store = SimpleNamespace(load_history_page=loader)
    backend = KernelBackend(kernel)
    page = await backend.conversation_history(before=50, session_id="session-1")
    loader.assert_awaited_once_with("session-1", limit=50, before=50, exclude_current_turn=False)
    assert page["messages"] == [{"content": "Bearer [redacted]"}]
    with pytest.raises(ValueError, match="active session changed"):
        await backend.conversation_history(session_id="other")

    async def switching(*args, **kwargs):
        kernel.session_id = "session-2"
        return {}

    kernel._store.load_history_page = switching
    with pytest.raises(ValueError, match="active session changed"):
        await backend.conversation_history(session_id="session-1")


@pytest.mark.asyncio
@pytest.mark.parametrize("verified", [True, False])
async def test_timed_out_preparation_restores_verified_review_without_execution(verified):
    import json

    saved = {
        "campaign_id": "campaign/saved",
        "manifest_digest": "sealed",
        "review": {"budget": {"turns": 10}},
        "approval_required": True,
    }

    class Store:
        async def load_messages(self, session_id):
            return [SimpleNamespace(role="tool", content=json.dumps(saved))] if verified else []

    class Campaigns:
        async def recover(self, **kwargs):
            pass

        async def list(self, session_id, *, limit):
            return [
                dict(
                    saved,
                    session_id=session_id,
                    status="prepared",
                    profile="quickscan",
                    objective="Test",
                )
            ]

        async def list_all(self):
            return await self.list("session-1", limit=None)

        async def execute(self, *args):
            raise AssertionError("Recovery must wait for explicit approval")

    kernel = FakeKernel(
        [
            app_event(
                "tool",
                name="assessment_prepare",
                state="failed",
                text="assessment_prepare timed out after 600 seconds",
            )
        ]
    )
    kernel._store = Store()
    backend = KernelBackend(kernel, campaigns=Campaigns())
    events = [event async for event in backend.stream("Continue preparation")]
    reviews = [json.loads(event.text) for event in events if event.kind == "campaign_review"]
    assert reviews == ([saved] if verified else [])
    assert kernel.submissions == ["Continue preparation"]
