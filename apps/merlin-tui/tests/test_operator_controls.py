import hashlib
import json
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from merlin_agents.campaign_service import AssessmentPrepareTool
from merlin_contracts import AppEvent, ChatMessage, ToolContext, ToolResult, ToolSpec
from merlin_tui.app import COMMAND_HINTS, COMMANDS, Composer
from merlin_tui.backend import DemoBackend, StreamEvent
from merlin_tui.bootstrap import build_app
from merlin_tui.kernel_backend import KernelBackend, event_to_stream
from merlin_tui.operator_tools import (
    CampaignConfigureTool,
    ConfiguredCampaignTool,
    OperatorCommandTool,
    OperatorRuntimeTool,
    _campaign_submit_payload,
    campaign_submit_envelope,
)


def context(**kwargs):
    return ToolContext(
        session_id="session-test", workspace_root="/tmp", call_id="call-test", **kwargs
    )


def saved(name, payload):
    return ChatMessage(role="tool", name=name, content=json.dumps(payload))


def test_campaign_submit_envelope_parser_requires_exact_v2_shape() -> None:
    request_id = "00000000-0000-4000-8000-000000000001"
    valid = campaign_submit_envelope("Assess target A", campaign_request_id=request_id)
    assert _campaign_submit_payload(valid) == {
        "campaign_request_id": request_id,
        "operator_objective": "Assess target A",
        "schema_version": "merlin.cli-campaign-submit.v2",
    }
    for payload in (
        {
            "operator_objective": "Assess target A",
            "schema_version": "merlin.cli-campaign-submit.v2",
        },
        {
            "campaign_request_id": 7,
            "operator_objective": "Assess target A",
            "schema_version": "merlin.cli-campaign-submit.v2",
        },
        {
            "campaign_request_id": request_id,
            "extra": True,
            "operator_objective": "Assess target A",
            "schema_version": "merlin.cli-campaign-submit.v2",
        },
    ):
        assert (
            _campaign_submit_payload("MERLIN_CAMPAIGN_SUBMIT_REQUEST\n" + json.dumps(payload))
            is None
        )


@pytest.mark.asyncio
async def test_campaign_configure_receipt_binds_latest_v2_request_id() -> None:
    campaign_request_id = "00000000-0000-4000-8000-000000000010"
    objective = "Assess https://target.test"
    history = [
        ChatMessage(
            role="user",
            content=campaign_submit_envelope(
                objective,
                campaign_request_id=campaign_request_id,
            ),
        )
    ]

    result = await CampaignConfigureTool(AsyncMock(return_value=tuple(history))).invoke(
        context(),
        {
            "new_campaign": True,
            "objective": objective,
            "campaign_profile": "recommended",
        },
    )

    assert not result.is_error
    receipt = json.loads(result.content)
    assert receipt["campaign_request_id"] == campaign_request_id
    assert "campaign_request_id" not in receipt["settings"]
    assert result.metadata["campaign_request_id"] == campaign_request_id


class Delegate:
    effect = "workspace_write"

    def __init__(self, name):
        self.name = name
        self.calls = []

    def describe(self):
        return ToolSpec(name=self.name, description="test", input_schema={})

    async def invoke(self, ctx, args):
        self.calls.append(dict(args))
        return ToolResult(content="ok")


@pytest.mark.asyncio
@pytest.mark.parametrize("profile", ["quickscan", "recommended", "exhaustive", "exploration"])
@pytest.mark.parametrize("mode", ["api", "hybrid", "browser"])
async def test_durable_profile_mode_target_applied_after_resume(profile, mode):
    history = []
    loader = AsyncMock(side_effect=lambda _: tuple(history))
    configure = CampaignConfigureTool(loader)
    result = await configure.invoke(
        context(),
        {
            "new_campaign": True,
            "objective": "Assess refund trap L3 at https://target.test",
            "campaign_profile": profile,
            "execution_mode": mode,
            "specialist_composition": "autonomous",
            "budget": {"max_turns": 5},
        },
    )
    assert not result.is_error
    history.append(saved("campaign_configure", json.loads(result.content)))
    # A new wrapper instance represents resumed execution; no in-memory intent survives.
    delegate = Delegate("assessment_prepare")
    resumed = ConfiguredCampaignTool(delegate, loader)
    await resumed.invoke(
        context(),
        {
            "objective": "old L4",
            "campaign_profile": "quickscan",
            "adapter": {"recon_handoff_id": "sealed-new"},
        },
    )
    assert delegate.calls == [
        {
            "objective": "Assess refund trap L3 at https://target.test",
            "campaign_profile": profile,
            "specialist_composition": "autonomous",
            "budget": {"max_turns": 5},
            "adapter": {"recon_handoff_id": "sealed-new", "execution_mode": mode},
        }
    ]


@pytest.mark.asyncio
async def test_configured_prepare_preserves_handoff_repair_metadata() -> None:
    class RepairDelegate(Delegate):
        async def invoke(self, ctx, args):
            self.calls.append(dict(args))
            return ToolResult(
                content="durable handoff required",
                is_error=True,
                metadata={
                    "retryable": True,
                    "repair_reason": "recon_handoff_required",
                    "preferred_next_tool": "browser_compile_api_handoff",
                    "preferred_next_tool_mode": "strict",
                },
            )

    campaign_request_id = "00000000-0000-4000-8000-000000000001"
    history = [
        ChatMessage(
            role="user",
            content=campaign_submit_envelope(
                "Assess https://target.test",
                campaign_request_id=campaign_request_id,
            ),
        ),
        saved(
            "campaign_configure",
            {
                "campaign_request_id": campaign_request_id,
                "status": "configured",
                "settings": {
                    "objective": "Assess https://target.test",
                    "campaign_profile": "recommended",
                },
            },
        ),
    ]
    delegate = RepairDelegate("assessment_prepare")
    result = await ConfiguredCampaignTool(delegate, AsyncMock(return_value=tuple(history))).invoke(
        context(),
        {
            "objective": "model-authored wrong target",
            "adapter": {"recon_handoff_id": "missing"},
        },
    )

    assert result.is_error is True
    assert result.metadata == {
        "retryable": True,
        "repair_reason": "recon_handoff_required",
        "preferred_next_tool": "browser_compile_api_handoff",
        "preferred_next_tool_mode": "strict",
        "repair_scope_campaign_request_id": campaign_request_id,
        "repair_scope_objective_digest": (
            "sha256:" + hashlib.sha256(b"Assess https://target.test").hexdigest()
        ),
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("execution_mode", "expected_tool"),
    [
        ("browser", "browser_record_api_handoff"),
        ("hybrid", "browser_compile_api_handoff"),
    ],
)
async def test_configured_prepare_routes_missing_durable_handoff_with_v2_scope(
    execution_mode: str, expected_tool: str
) -> None:
    class Service:
        async def prepare(self, *_args, **_kwargs):
            raise AssertionError("missing handoff must fail before campaign preparation")

    objective = "Assess https://target.test"
    campaign_request_id = "00000000-0000-4000-8000-000000000003"
    history = [
        ChatMessage(
            role="user",
            content=campaign_submit_envelope(
                objective,
                campaign_request_id=campaign_request_id,
            ),
        ),
        saved(
            "campaign_configure",
            {
                "campaign_request_id": campaign_request_id,
                "status": "configured",
                "settings": {
                    "objective": objective,
                    "campaign_profile": "recommended",
                    "execution_mode": execution_mode,
                },
            },
        ),
    ]
    delegate = AssessmentPrepareTool(Service())  # type: ignore[arg-type]

    result = await ConfiguredCampaignTool(delegate, AsyncMock(return_value=tuple(history))).invoke(
        context(),
        {
            "objective": "model-authored wrong target",
            "adapter": {"interface": "browser"},
        },
    )

    assert result.is_error is True
    assert result.content == (
        "browser/hybrid preparation requires adapter.recon_handoff_id from a durable "
        "reconnaissance handoff"
    )
    assert result.metadata == {
        "retryable": True,
        "repair_reason": "recon_handoff_required",
        "preferred_next_tool": expected_tool,
        "preferred_next_tool_mode": "strict",
        "repair_scope_campaign_request_id": campaign_request_id,
        "repair_scope_objective_digest": (
            "sha256:" + hashlib.sha256(objective.encode()).hexdigest()
        ),
    }


@pytest.mark.asyncio
async def test_configured_prepare_rejects_request_id_bound_to_stale_settings() -> None:
    campaign_request_id = "00000000-0000-4000-8000-000000000002"
    history = [
        saved(
            "campaign_configure",
            {
                "status": "configured",
                "settings": {
                    "objective": "Assess target A",
                    "campaign_profile": "recommended",
                },
            },
        ),
        ChatMessage(
            role="user",
            content=campaign_submit_envelope(
                "Assess target B",
                campaign_request_id=campaign_request_id,
            ),
        ),
    ]
    delegate = Delegate("assessment_prepare")

    result = await ConfiguredCampaignTool(delegate, AsyncMock(return_value=tuple(history))).invoke(
        context(), {"objective": "Assess target B", "adapter": {}}
    )

    assert result.is_error is True
    assert "does not match the configured operator request" in result.content
    assert result.metadata == {}
    assert delegate.calls == []


@pytest.mark.asyncio
async def test_configured_prepare_rejects_new_v2_id_with_same_objective() -> None:
    objective = "Assess https://target.test"
    request_a = "00000000-0000-4000-8000-000000000011"
    request_b = "00000000-0000-4000-8000-000000000012"
    history = [
        ChatMessage(
            role="user",
            content=campaign_submit_envelope(objective, campaign_request_id=request_a),
        ),
        saved(
            "campaign_configure",
            {
                "campaign_request_id": request_a,
                "status": "configured",
                "settings": {
                    "objective": objective,
                    "campaign_profile": "exhaustive",
                    "execution_mode": "hybrid",
                    "budget": {"max_turns": 17},
                },
            },
        ),
        ChatMessage(
            role="user",
            content=campaign_submit_envelope(objective, campaign_request_id=request_b),
        ),
    ]
    delegate = Delegate("assessment_prepare")

    result = await ConfiguredCampaignTool(delegate, AsyncMock(return_value=tuple(history))).invoke(
        context(), {"objective": objective, "adapter": {}}
    )

    assert result.is_error is True
    assert "does not match the configured operator request" in result.content
    assert result.metadata == {}
    assert delegate.calls == []


@pytest.mark.asyncio
async def test_configured_prepare_same_v2_id_retry_retains_configuration() -> None:
    objective = "Assess https://target.test"
    campaign_request_id = "00000000-0000-4000-8000-000000000013"
    boundary = ChatMessage(
        role="user",
        content=campaign_submit_envelope(objective, campaign_request_id=campaign_request_id),
    )
    history = [
        boundary,
        saved(
            "campaign_configure",
            {
                "campaign_request_id": campaign_request_id,
                "status": "configured",
                "settings": {
                    "objective": objective,
                    "campaign_profile": "exhaustive",
                    "execution_mode": "hybrid",
                    "budget": {"max_turns": 17},
                },
            },
        ),
        boundary,
    ]
    delegate = Delegate("assessment_prepare")

    result = await ConfiguredCampaignTool(delegate, AsyncMock(return_value=tuple(history))).invoke(
        context(), {"objective": "model retry", "adapter": {}}
    )

    assert not result.is_error
    assert delegate.calls == [
        {
            "objective": objective,
            "campaign_profile": "exhaustive",
            "budget": {"max_turns": 17},
            "adapter": {"execution_mode": "hybrid"},
        }
    ]


@pytest.mark.asyncio
async def test_settings_revision_preserves_unspecified_fields_and_new_scan_resets_them():
    initial = {
        "status": "configured",
        "settings": {
            "objective": "L3",
            "campaign_profile": "exploration",
            "execution_mode": "hybrid",
            "specialist_composition": "autonomous",
            "budget": {"max_turns": 10},
        },
    }
    history = [saved("campaign_configure", initial)]
    loader = AsyncMock(side_effect=lambda _: tuple(history))
    tool = CampaignConfigureTool(loader)
    revised = await tool.invoke(context(), {"campaign_profile": "exhaustive", "budget": None})
    settings = json.loads(revised.content)["settings"]
    assert settings == {
        "objective": "L3",
        "campaign_profile": "exhaustive",
        "execution_mode": "hybrid",
        "specialist_composition": "autonomous",
    }
    history.append(saved("campaign_configure", json.loads(revised.content)))
    new = await tool.invoke(
        context(), {"new_campaign": True, "objective": "L2", "campaign_profile": "quickscan"}
    )
    assert json.loads(new.content)["settings"] == {
        "objective": "L2",
        "campaign_profile": "quickscan",
    }
    assert new.metadata["preferred_next_tool"] == "browser_recall_reconnaissance"
    assert new.metadata["preferred_next_tool_mode"] == "after_one_admitted_action"


@pytest.mark.asyncio
async def test_different_v2_request_omission_starts_fresh_settings() -> None:
    objective_a = "Assess https://a.test"
    objective_b = "Assess https://b.test"
    request_a = "00000000-0000-4000-8000-000000000020"
    request_b = "00000000-0000-4000-8000-000000000021"
    history = [
        ChatMessage(
            role="user",
            content=campaign_submit_envelope(objective_a, campaign_request_id=request_a),
        ),
        saved(
            "campaign_configure",
            {
                "campaign_request_id": request_a,
                "status": "configured",
                "settings": {
                    "objective": objective_a,
                    "campaign_profile": "exhaustive",
                    "execution_mode": "hybrid",
                    "specialist_composition": "autonomous",
                    "budget": {"max_turns": 17},
                },
            },
        ),
        ChatMessage(
            role="user",
            content=campaign_submit_envelope(objective_b, campaign_request_id=request_b),
        ),
    ]

    result = await CampaignConfigureTool(AsyncMock(return_value=tuple(history))).invoke(
        context(), {"campaign_profile": "recommended"}
    )

    assert not result.is_error
    receipt = json.loads(result.content)
    assert receipt["campaign_request_id"] == request_b
    assert receipt["settings"] == {
        "objective": objective_b,
        "campaign_profile": "recommended",
    }
    assert result.metadata["preferred_next_tool"] == "browser_recall_reconnaissance"


@pytest.mark.asyncio
async def test_same_v2_request_partial_update_retains_settings() -> None:
    objective = "Assess https://target.test"
    campaign_request_id = "00000000-0000-4000-8000-000000000022"
    history = [
        ChatMessage(
            role="user",
            content=campaign_submit_envelope(
                objective,
                campaign_request_id=campaign_request_id,
            ),
        ),
        saved(
            "campaign_configure",
            {
                "campaign_request_id": campaign_request_id,
                "status": "configured",
                "settings": {
                    "objective": objective,
                    "campaign_profile": "exhaustive",
                    "execution_mode": "hybrid",
                    "specialist_composition": "autonomous",
                    "budget": {"max_turns": 17},
                },
            },
        ),
    ]

    result = await CampaignConfigureTool(AsyncMock(return_value=tuple(history))).invoke(
        context(), {"campaign_profile": "quickscan"}
    )

    assert not result.is_error
    receipt = json.loads(result.content)
    assert receipt["campaign_request_id"] == campaign_request_id
    assert receipt["settings"] == {
        "objective": objective,
        "campaign_profile": "quickscan",
        "execution_mode": "hybrid",
        "specialist_composition": "autonomous",
        "budget": {"max_turns": 17},
    }


@pytest.mark.asyncio
async def test_new_campaign_defaults_to_recommended_when_profile_is_unspecified():
    tool = CampaignConfigureTool(AsyncMock(return_value=()))

    result = await tool.invoke(
        context(),
        {"new_campaign": True, "objective": "Assess https://target.test"},
    )

    assert not result.is_error
    assert json.loads(result.content)["settings"] == {
        "objective": "Assess https://target.test",
        "campaign_profile": "recommended",
    }
    assert result.metadata["preferred_next_tool"] == "browser_recall_reconnaissance"
    assert result.metadata["preferred_next_tool_mode"] == "after_one_admitted_action"


@pytest.mark.asyncio
async def test_new_campaign_preserves_operator_objective_and_rejects_inferred_profile():
    exact = (
        "Use Merlin to test https://target.test.\n\nCover every tier and every available character."
    )
    history = [ChatMessage(role="user", content=exact)]
    tool = CampaignConfigureTool(AsyncMock(return_value=tuple(history)))

    result = await tool.invoke(
        context(),
        {
            "new_campaign": True,
            "objective": "Full coverage of https://target.test",
            "campaign_profile": "exhaustive",
        },
    )

    assert not result.is_error
    assert json.loads(result.content)["settings"] == {
        "objective": exact,
        "campaign_profile": "recommended",
    }


@pytest.mark.asyncio
async def test_new_campaign_extracts_typed_cli_objective_from_internal_prompt():
    exact = (
        "Assess only https://target.test as an authorized challenge. "
        "Do not access another target. Mention MERLIN_CAMPAIGN_SUBMIT_REQUEST\n"
        "as plain test data."
    )
    envelope = json.dumps(
        {
            "operator_objective": exact,
            "schema_version": "merlin.cli-campaign-submit.v1",
        },
        sort_keys=True,
    )
    synthetic_prompt = (
        f"{exact}\nMERLIN_CAMPAIGN_SUBMIT_REQUEST\n{envelope}\n"
        "The operator explicitly selected campaign profile: exploration; do not ask again.\n"
        "Pass the operator objective above verbatim to assessment_prepare."
    )
    retry_prompt = (
        "The previous prepared campaign was cancelled. Reuse the sealed reconnaissance for "
        "https://target.test and call assessment_prepare now."
    )
    history = [
        ChatMessage(role="user", content=synthetic_prompt),
        ChatMessage(role="user", content=retry_prompt),
    ]
    tool = CampaignConfigureTool(AsyncMock(return_value=tuple(history)))

    result = await tool.invoke(
        context(),
        {
            "new_campaign": True,
            "objective": synthetic_prompt,
            "campaign_profile": "recommended",
        },
    )

    assert not result.is_error
    assert json.loads(result.content)["settings"] == {
        "objective": exact,
        "campaign_profile": "exploration",
    }
    history.append(saved("campaign_configure", json.loads(result.content)))
    delegate = Delegate("assessment_prepare")
    configured = ConfiguredCampaignTool(delegate, AsyncMock(return_value=tuple(history)))

    await configured.invoke(
        context(),
        {
            "objective": exact,
            "campaign_profile": "exploration",
            "adapter": {"recon_handoff_id": "sealed-live"},
        },
    )

    assert delegate.calls == [
        {
            "objective": exact,
            "campaign_profile": "exploration",
            "adapter": {"recon_handoff_id": "sealed-live"},
        }
    ]


@pytest.mark.asyncio
async def test_new_campaign_restores_operator_url_when_model_omits_it():
    exact = "Run a redpen scan on https://break-room.ai, Bank L1"
    history = [ChatMessage(role="user", content=exact)]
    tool = CampaignConfigureTool(AsyncMock(return_value=tuple(history)))

    result = await tool.invoke(
        context(),
        {
            "new_campaign": True,
            "objective": "Assess Bank L1",
            "campaign_profile": "exploration",
        },
    )

    assert not result.is_error
    assert json.loads(result.content)["settings"] == {
        "objective": exact,
        "campaign_profile": "recommended",
    }


@pytest.mark.asyncio
async def test_new_campaign_honors_operator_named_profile():
    exact = "Use the exhaustive campaign profile for https://target.test."
    history = [ChatMessage(role="user", content=exact)]
    tool = CampaignConfigureTool(AsyncMock(return_value=tuple(history)))

    result = await tool.invoke(
        context(),
        {
            "new_campaign": True,
            "objective": "Assess https://target.test",
            "campaign_profile": "exhaustive",
        },
    )

    assert not result.is_error
    assert json.loads(result.content)["settings"] == {
        "objective": exact,
        "campaign_profile": "exhaustive",
    }


@pytest.mark.asyncio
async def test_new_campaign_honors_structured_operator_profile_selection():
    exact = "Run a redpen scan on https://break-room.ai, the bank L1"
    response = {
        "request_id": "profile-choice",
        "answers": {
            "profile": {"option_ids": ["exploration"], "text": ""},
            "composition": {"option_ids": ["autonomous"], "text": ""},
        },
    }
    history = [
        ChatMessage(role="user", content=exact),
        ChatMessage(
            role="user",
            content="MERLIN_USER_INPUT_RESPONSE\n" + json.dumps(response),
        ),
    ]
    tool = CampaignConfigureTool(AsyncMock(return_value=tuple(history)))

    result = await tool.invoke(
        context(),
        {
            "new_campaign": True,
            "objective": "Assess Bank L1",
            "campaign_profile": "exploration",
        },
    )

    assert not result.is_error
    assert json.loads(result.content)["settings"] == {
        "objective": exact,
        "campaign_profile": "exploration",
    }


@pytest.mark.asyncio
async def test_campaign_setting_update_does_not_restart_reconnaissance():
    history = [
        saved(
            "campaign_configure",
            {
                "status": "configured",
                "settings": {
                    "campaign_profile": "recommended",
                    "objective": "existing target",
                },
            },
        )
    ]
    tool = CampaignConfigureTool(AsyncMock(side_effect=lambda _: tuple(history)))

    result = await tool.invoke(context(), {"campaign_profile": "exploration"})

    assert not result.is_error
    assert "preferred_next_tool" not in result.metadata


@pytest.mark.asyncio
async def test_no_recon_when_route_preflight_fails_and_worker_cannot_configure():
    delegate = AsyncMock(return_value=(False, "worker route missing"))
    tool = CampaignConfigureTool(AsyncMock(return_value=()), delegate)
    result = await tool.invoke(
        context(), {"new_campaign": True, "objective": "L3", "campaign_profile": "exploration"}
    )
    assert result.is_error
    assert "not_configured" in result.content
    denied = await tool.invoke(context(agent_id="worker"), {"campaign_profile": "quickscan"})
    assert denied.is_error
    assert delegate.await_count == 1


@pytest.mark.asyncio
async def test_top_level_model_cannot_execute_campaign_before_operator_approval():
    review = {"campaign_id": "campaign/L3", "manifest_digest": "sha256:new"}
    history = [saved("assessment_prepare", review)]
    delegate = Delegate("assessment_execute")
    wrapper = ConfiguredCampaignTool(delegate, AsyncMock(side_effect=lambda _: tuple(history)))
    mismatched = await wrapper.invoke(
        context(), {"campaign_id": "campaign/L4", "manifest_digest": "sha256:old"}
    )
    assert mismatched.is_error
    assert "superseded" in mismatched.content
    assert "campaign/L3" in mismatched.content
    assert not delegate.calls
    denied = await wrapper.invoke(context(), review)
    assert denied.is_error
    assert "explicit approval" in denied.content
    assert not delegate.calls
    history.append(
        saved(
            "campaign_configure",
            {"status": "configured", "settings": {"campaign_profile": "exploration"}},
        )
    )
    assert (await wrapper.invoke(context(), review)).is_error
    assert not delegate.calls


@pytest.mark.asyncio
async def test_internal_campaign_protocol_can_execute_reviewed_manifest():
    review = {"campaign_id": "campaign/L3", "manifest_digest": "sha256:new"}
    delegate = Delegate("assessment_execute")
    wrapper = ConfiguredCampaignTool(delegate, AsyncMock(return_value=()))

    result = await wrapper.invoke(context(agent_id="worker/1"), review)

    assert not result.is_error
    assert delegate.calls == [review]


@pytest.mark.asyncio
async def test_command_catalogue_parity_and_rejection_of_shell_and_worker_requests():
    tool = OperatorCommandTool(COMMANDS, COMMAND_HINTS)
    description = tool.describe().description
    assert all(command in description for command in COMMANDS)
    for command in (
        "/model",
        "/setup jev",
        "/new",
        "/report --format json",
        "/pause campaign campaign/exact",
        "/skills",
    ):
        result = await tool.invoke(context(), {"command": command})
        assert json.loads(result.content) == {
            "status": "queued",
            "command": command,
            "request_id": "call-test",
        }
    for command in ("rm -rf /", "/bogus", "/model\n/quit"):
        assert (await tool.invoke(context(), {"command": command})).is_error
    assert (await tool.invoke(context(agent_id="worker"), {"command": "/new"})).is_error


@pytest.mark.asyncio
async def test_runtime_controls_reuse_backend_and_reject_cross_session_or_unknown_methods():
    backend = SimpleNamespace(
        kernel=SimpleNamespace(session_id="session-test"),
        pause_campaign=AsyncMock(return_value={"status": "paused"}),
    )
    tool = OperatorRuntimeTool(backend, read_only=False)
    result = await tool.invoke(
        context(), {"operation": "pause_campaign", "arguments": {"campaign_id": "campaign/exact"}}
    )
    assert not result.is_error
    backend.pause_campaign.assert_awaited_once_with(campaign_id="campaign/exact")
    assert (await tool.invoke(context(), {"operation": "execute_campaign"})).is_error
    backend.kernel.session_id = "another-session"
    assert (await tool.invoke(context(), {"operation": "pause_campaign"})).is_error


@pytest.mark.asyncio
async def test_freeform_instruction_invalidates_pending_approval_and_reaches_model():
    class Backend(DemoBackend):
        prompts = []

        async def stream(self, prompt):
            self.prompts.append(prompt)
            yield StreamEvent("content", "Preparing your replacement scan")

    backend = Backend()
    app = build_app(backend)
    async with app.run_test() as pilot:
        await pilot.pause()
        app._pending_choice = (("Approve", "approve"), ("Revise", "revise"))
        app._pending_choice_kind = "campaign_review"
        app._pending_campaign_review = ("campaign/old-L4", "sha256:old")
        app.query_one("#composer", Composer).text = "Use exploration on L3 instead"
        await app.action_send()
        await pilot.pause()
        assert backend.prompts == ["Use exploration on L3 instead"]
        assert app._pending_campaign_review is None
        assert not app._pending_choice


@pytest.mark.asyncio
async def test_chat_commands_dispatch_after_response_once_through_existing_handler():
    request = json.dumps({"status": "queued", "request_id": "chat-control", "command": "/model"})

    class Backend(DemoBackend):
        async def stream(self, prompt):
            yield StreamEvent("operator_command", request)
            yield StreamEvent("content", "Opening model settings")

    app = build_app(Backend())
    async with app.run_test() as pilot:
        await pilot.pause()
        original = app._run_command
        calls = []

        async def dispatch(command):
            assert app._stream_task is None
            assert any(entry.text == "Opening model settings" for entry in app._transcript_entries)
            calls.append(command)
            await original(command)

        app._run_command = dispatch
        await app._stream("Open model settings")
        await pilot.pause()
        assert calls == ["/model"]
        await app._stream("Replay the same delivery")
        await pilot.pause()
        assert calls == ["/model"]


def test_control_event_translation_uses_only_successful_tool_receipts():
    for name, status in (("operator_command", "queued"), ("campaign_configure", "configured")):
        event = AppEvent(
            type="tool",
            occurred_at=datetime.now(UTC),
            payload={"name": name, "state": "completed", "text": json.dumps({"status": status})},
        )
        assert event_to_stream(event).kind == name
        failed = event.model_copy(update={"payload": {**event.payload, "state": "failed"}})
        assert event_to_stream(failed).kind == "tool"


@pytest.mark.asyncio
async def test_production_chat_path_does_not_require_exact_profile_keywords():
    class Kernel:
        session_id = "session-test"
        turn_active = False
        submissions = []

        async def submit(self, prompt):
            self.submissions.append(prompt)

        async def next_event(self):
            raise TimeoutError

    kernel = Kernel()
    backend = KernelBackend(kernel, operator_controls=True)
    prompt = "Run a full redpen scan on https://target.test with a self-selected team"
    assert [event async for event in backend.stream(prompt)] == []
    encoded = kernel.submissions[0].split("MERLIN_CAMPAIGN_SUBMIT_REQUEST\n", 1)[1]
    envelope = json.loads(encoded)
    assert kernel.submissions == [
        f"{prompt}\n"
        + campaign_submit_envelope(
            prompt,
            campaign_request_id=envelope["campaign_request_id"],
        )
    ]
    ordinary = "Explain the current runtime status"
    assert [event async for event in backend.stream(ordinary)] == []
    assert kernel.submissions[-1] == ordinary


@pytest.mark.asyncio
@pytest.mark.parametrize("resume_fails", [False, True])
async def test_browser_handback_result_reaches_model_and_continues_same_turn(
    resume_fails, monkeypatch
):
    monkeypatch.setattr("merlin_kernel.kernel._load_jev_client", lambda: None)
    from merlin_contracts import ModelCompleted, TextDelta, ToolCall
    from merlin_kernel import Kernel
    from merlin_tools import ToolBroker

    class Store:
        def __init__(self):
            self.messages = []

        async def create_session(self, **kwargs):
            return "session-test"

        async def load_messages(self, session_id):
            return self.messages

        async def append_message(self, session_id, message):
            self.messages.append(message)

        async def append_event(self, session_id, event):
            pass

    calls = []
    browser = SimpleNamespace(
        resume=AsyncMock(
            return_value={"context_id": "browser-exact", "worker_id": "agent:browser"},
            side_effect=RuntimeError("Return to the target before resuming")
            if resume_fails
            else None,
        ),
        list_tabs=AsyncMock(return_value=[]),
    )

    class Provider:
        async def stream(self, request):
            calls.append(request)
            if len(calls) == 1:
                yield ToolCall(
                    call_id="old-path",
                    name="operator_command",
                    arguments={"command": "/browser resume browser-exact"},
                )
            elif len(calls) == 2:
                assert "resume_browser_context" in request.messages[-1].content
                yield ToolCall(
                    call_id="hand-back",
                    name="operator_runtime",
                    arguments={
                        "operation": "resume_browser_context",
                        "arguments": {"context_id": "browser-exact"},
                    },
                )
            elif len(calls) == 3:
                result = json.loads(request.messages[-1].content)
                if resume_fails:
                    assert result["status"] == "failed"
                    yield TextDelta(text="Browser is still yours: return to the target first.")
                else:
                    assert result["status"] == "completed"
                    assert result["result"]["owner"] == "agent:browser"
                    yield ToolCall(call_id="verify-session", name="browser_snapshot", arguments={})
            else:
                assert request.messages[-1].content == "Authenticated target session observed"
                yield TextDelta(text="Session verified; reconnaissance continues.")
            yield ModelCompleted()

    class Snapshot:
        effect = "workspace_read"

        def describe(self):
            return ToolSpec(
                name="browser_snapshot", description="Fixture observation", input_schema={}
            )

        async def invoke(self, ctx, args):
            browser.resume.assert_awaited_once_with("session-test", "browser-exact")
            return ToolResult(content="Authenticated target session observed")

    broker, store = ToolBroker(), Store()
    settings = SimpleNamespace(get_model=AsyncMock(return_value="fixture-model"))
    kernel = Kernel(Provider(), store, broker, settings)
    backend = KernelBackend(kernel, browser=browser, operator_controls=True)
    broker.register(OperatorCommandTool(COMMANDS, COMMAND_HINTS))
    broker.register(OperatorRuntimeTool(backend, read_only=False))
    broker.register(Snapshot())
    await kernel.start()
    await kernel.submit("Signed in. Hand the browser back and continue reconnaissance.")
    await kernel.wait_for_turn()
    assert len(calls) == (3 if resume_fails else 4)
    assert len([message for message in store.messages if message.role == "user"]) == 1
    assert store.messages[-1].content == (
        "Browser is still yours: return to the target first."
        if resume_fails
        else "Session verified; reconnaissance continues."
    )
    browser.resume.assert_awaited_once_with("session-test", "browser-exact")
    if resume_fails:
        browser.list_tabs.assert_not_awaited()
