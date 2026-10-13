import asyncio
import json
import re
from types import SimpleNamespace

import pytest
from merlin_contracts import (
    AgentProgress,
    AgentSummary,
    AgentTask,
    ChatMessage,
    DynamicToolOutput,
    Finding,
    InfrastructureFailureCategory,
    MailboxDelivery,
    ModelCompleted,
    ModelError,
    ModelUsage,
    ProviderFailureKind,
    ReasoningDelta,
    ReportResult,
    TextDelta,
    ToolCall,
    ToolResult,
    ToolSpec,
)
from merlin_kernel import CommandError, Kernel, parse_command
from merlin_kernel.kernel import (
    _automatic_recon_continuation,
    _KernelJevClient,
    _restored_automatic_recon,
)


@pytest.mark.asyncio
async def test_followup_during_generation_gets_answer_without_cancelling_turn():
    started, release = asyncio.Event(), asyncio.Event()
    requests = []

    class ReconProvider:
        async def stream(self, request):
            requests.append(request)
            if len(requests) == 1:
                started.set()
                await release.wait()
                yield TextDelta(text="Initial progress")
            elif len(requests) == 2:
                yield TextDelta(text="Yes, I am mapping the authenticated routes.")
            else:
                assert any("Now continue" in m.content for m in request.messages)
                yield TextDelta(text="Reconnaissance is continuing on the existing target.")
            yield ModelCompleted()

    store = Store()
    kernel = Kernel(ReconProvider(), store, Tools(), Settings())
    await kernel.start()
    await kernel.submit("Recon https://example.test authenticated routes")
    await started.wait()
    original_turn = kernel._turn
    assert await kernel.steer("What are you checking now?")
    assert await kernel.steer("Keep the existing target please.")
    assert kernel._turn is original_turn and not original_turn.cancelled()
    release.set()
    await kernel.wait_for_turn()
    assert len(requests) == 3
    users = [m.content for m in requests[-1].messages if m.role == "user"]
    assert users == [
        "Recon https://example.test authenticated routes",
        "What are you checking now?",
        "Keep the existing target please.",
    ]
    assert store.messages[kernel.session_id][-1].content.startswith("Reconnaissance is continuing")
    assert not await kernel.steer("idle message")
    assert "idle message" not in [m.content for m in store.messages[kernel.session_id]]


@pytest.mark.asyncio
async def test_followup_during_recon_tool_preserves_pairing_and_target():
    started, release = asyncio.Event(), asyncio.Event()
    requests = []

    class ReconTools(Tools):
        def describe(self):
            return (ToolSpec(name="knowledge_search", description="Search", input_schema={}),)

        async def invoke(self, name, context, arguments):
            started.set()
            await release.wait()
            return ToolResult(content="Existing target inventory")

    class ReconProvider:
        async def stream(self, request):
            requests.append(request)
            if len(requests) == 1:
                yield ToolCall(call_id="inventory", name="knowledge_search", arguments={})
                yield ModelCompleted(finish_reason="tool_calls")
            elif len(requests) == 2:
                transcript = [m for m in request.messages if m.role != "system"]
                assert [m.role for m in transcript] == ["user", "assistant", "tool", "user"]
                assert transcript[-1].content == "Why are you searching that?"
                assert "example.test" in transcript[0].content
                yield TextDelta(text="I am checking relevant guidance before continuing recon.")
                yield ModelCompleted()
            else:
                assert any("Now continue" in m.content for m in request.messages)
                yield TextDelta(text="Reconnaissance continues without repeating the search.")
                yield ModelCompleted()

    store = Store()
    kernel = Kernel(ReconProvider(), store, ReconTools(), Settings())
    await kernel.start()
    await kernel.submit("Recon https://example.test")
    await started.wait()
    assert await kernel.steer("Why are you searching that?")
    release.set()
    await kernel.wait_for_turn()
    assert len(requests) == 3
    # Durable receipt order stays chronological; provider projection repairs only adjacency.
    assert [m.role for m in store.messages[kernel.session_id]][:4] == [
        "user",
        "assistant",
        "user",
        "tool",
    ]
    assert store.messages[kernel.session_id][-1].role == "assistant"
    assert any(e.payload.get("foreground_followup") for e in store.events)


class Store:
    def __init__(self):
        self.messages, self.events = {}, []

    async def create_session(self, **_):
        session = f"s{len(self.messages) + 1}"
        self.messages[session] = []
        return session

    async def list_sessions(self):
        return [{"id": key} for key in self.messages]

    async def session_exists(self, session_id):
        return session_id in self.messages

    async def load_messages(self, session_id):
        return self.messages[session_id]

    async def append_message(self, session_id, message):
        self.messages[session_id].append(message)

    async def append_event(self, session_id, event):
        self.events.append(event)

    async def clear_session(self, session_id):
        self.messages[session_id].clear()

    async def branch_session(self, session_id, sequence):
        branch_id = f"{session_id}-branch-{sequence}"
        self.messages[branch_id] = list(self.messages[session_id][:sequence])
        return branch_id

    async def rewind_session(self, session_id, sequence):
        self.projection_head = (session_id, sequence)

    async def restore_session(self, session_id):
        self.projection_head = (session_id, None)


class Settings:
    model = "test-model"

    async def setup(self):
        return {"configured": True}

    async def get_model(self):
        return self.model

    async def set_model(self, model):
        self.model = model

    async def status(self):
        return {"configured": True}

    async def plugins(self):
        return []


class Provider:
    async def stream(self, request):
        yield TextDelta(text="hello")
        yield ModelCompleted()


@pytest.mark.asyncio
async def test_provider_request_has_bounded_output_budget() -> None:
    requests = []

    class RecordingProvider:
        async def stream(self, request):
            requests.append(request)
            yield TextDelta(text="done")
            yield ModelCompleted()

    kernel = Kernel(RecordingProvider(), Store(), Tools(), Settings())
    await kernel.start()
    await kernel.submit("hello")
    await kernel.wait_for_turn()

    assert requests[0].max_output_tokens == 8_192


@pytest.mark.asyncio
async def test_provider_request_uses_catalogued_model_output_limit() -> None:
    from merlin_providers import ModelCapabilityRegistry

    requests = []

    class RecordingProvider:
        async def stream(self, request):
            requests.append(request)
            yield TextDelta(text="done")
            yield ModelCompleted()

    settings = Settings()
    settings.model = "deepseek-v4-flash"
    kernel = Kernel(
        RecordingProvider(),
        Store(),
        Tools(),
        settings,
        capability_registry=ModelCapabilityRegistry(),
    )
    await kernel.start()
    await kernel.submit("hello")
    await kernel.wait_for_turn()

    assert requests[0].max_output_tokens == 393_216


@pytest.mark.asyncio
async def test_explicit_kernel_output_limit_caps_catalogued_limit() -> None:
    from merlin_providers import ModelCapabilityRegistry

    requests = []

    class RecordingProvider:
        async def stream(self, request):
            requests.append(request)
            yield TextDelta(text="done")
            yield ModelCompleted()

    settings = Settings()
    settings.model = "deepseek-v4-flash"
    kernel = Kernel(
        RecordingProvider(),
        Store(),
        Tools(),
        settings,
        capability_registry=ModelCapabilityRegistry(),
        max_output_tokens_per_request=16_384,
    )
    await kernel.start()
    await kernel.submit("hello")
    await kernel.wait_for_turn()

    assert requests[0].max_output_tokens == 16_384


class Tools:
    def describe(self):
        return ()

    async def invoke(self, name, context, arguments):
        return ToolResult(content="ok")


@pytest.mark.asyncio
@pytest.mark.parametrize("dynamic", [False, True])
async def test_large_prepared_review_survives_real_broker_text_bound(dynamic) -> None:
    from merlin_tools import ToolBroker

    prepared = {
        "campaign_id": "campaign/large",
        "manifest_digest": "sha256:sealed",
        "approval_required": True,
        "review": {
            "budget": {"turns": None},
            "cross_campaign_learning": {
                "new": 3000,
                "decisions": [{"basis": "x" * 1200}] * 3000,
            },
            "target_contract": {"execution_mode": "api", "execution_options": ["api"]},
        },
    }

    class PrepareTool:
        def describe(self):
            return ToolSpec(name="assessment_prepare", description="Prepare", input_schema={})

        async def invoke(self, context, arguments):
            return ToolResult(content=json.dumps(prepared), metadata=prepared)

    call = ToolCall(call_id="large-prepare", name="assessment_prepare", arguments={})

    class PreparingProvider:
        def __init__(self):
            self.calls = 0

        async def stream(self, request):
            self.calls += 1
            if self.calls == 1:
                yield call
                yield ModelCompleted(finish_reason="tool_calls")
            else:
                yield TextDelta(text="Please review and approve.")
                yield ModelCompleted()

    class DynamicPreparingProvider(PreparingProvider):
        async def stream_with_tools(self, request, handler):
            output = await handler(call)
            assert output.success is True
            with pytest.raises(json.JSONDecodeError):
                json.loads(output.content)
            yield TextDelta(text="Please review and approve.")
            yield ModelCompleted()

    store = Store()
    broker = ToolBroker([PrepareTool()])
    kernel = Kernel(
        DynamicPreparingProvider() if dynamic else PreparingProvider(), store, broker, Settings()
    )
    await kernel.start()
    await kernel.submit("prepare")
    await kernel.wait_for_turn()

    completed = next(
        event
        for event in store.events
        if event.type == "tool" and event.payload.get("state") == "completed"
    )
    with pytest.raises(json.JSONDecodeError):
        json.loads(completed.payload["text"])
    review = completed.payload["campaign_review"]
    assert review["campaign_id"] == prepared["campaign_id"]
    assert review["manifest_digest"] == prepared["manifest_digest"]
    assert review["review"]["cross_campaign_learning"] == {"new": 3000}
    assert len(json.dumps(review)) < 1024
    assert len(prepared["review"]["cross_campaign_learning"]["decisions"]) == 3000
    assert not any(event.payload.get("name") == "assessment_execute" for event in store.events)


_DISCOVERY_TOOLS = [
    "browser_snapshot",
    "browser_network_events",
    "browser_compile_api_handoff",
    "assessment_prepare",
]
_OWNED_DISCOVERY_TOOLS = [*_DISCOVERY_TOOLS, "browser_end_owned_session"]


def _cleanup_continuation(
    handoff_id: str,
    *,
    ownership: str = "unknown",
    ownership_evidence_refs: list[str] | None = None,
) -> dict[str, object]:
    refs = list(ownership_evidence_refs or [])
    failure_code = "cleanup_discovery_required" if ownership == "owned" else "session_not_owned"
    return {
        "kind": "cleanup_discovery",
        "status": "required",
        "progress_key": f"{handoff_id}:{failure_code}",
        "stage": "compile_superseding_lifecycle_handoff",
        "handoff_id": handoff_id,
        "session_ownership": ownership,
        "ownership_evidence_refs": refs,
        "evidence_refs": [],
        "allowed_tools": (_OWNED_DISCOVERY_TOOLS if ownership == "owned" else _DISCOVERY_TOOLS),
    }


def test_owned_cleanup_continuation_requires_creation_evidence() -> None:
    continuation = _cleanup_continuation("handoff:no-evidence", ownership="owned")
    result = ToolResult(
        content="held",
        is_error=True,
        metadata={"automatic_recon_continuation": continuation},
    )

    assert _automatic_recon_continuation(result) is None


def test_cleanup_continuation_requires_canonical_ownership_name_and_exact_fields() -> None:
    not_owned = _cleanup_continuation("handoff:legacy-name")
    not_owned["session_ownership"] = "not_owned"
    extra_field = _cleanup_continuation("handoff:extra-field")
    extra_field["model_claim"] = "owned"

    for continuation in (not_owned, extra_field):
        result = ToolResult(
            content="held",
            is_error=True,
            metadata={"automatic_recon_continuation": continuation},
        )
        assert _automatic_recon_continuation(result) is None


def test_restored_cleanup_continuation_rejects_mutated_owned_tool_set() -> None:
    continuation = _cleanup_continuation(
        "handoff:owned",
        ownership="owned",
        ownership_evidence_refs=["creation:before", "creation:after"],
    )
    continuation["allowed_tools"] = [*_OWNED_DISCOVERY_TOOLS, "browser_click"]
    transcript = [
        ChatMessage(
            role="assistant",
            content="",
            tool_calls=[
                {
                    "id": "prepare-owned",
                    "type": "function",
                    "function": {"name": "assessment_prepare", "arguments": "{}"},
                }
            ],
        ),
        ChatMessage(
            role="tool",
            name="assessment_prepare",
            tool_call_id="prepare-owned",
            content=(
                "held\n[merlin-automatic-recon]"
                + json.dumps(
                    {"state": "held", "continuation": continuation},
                    sort_keys=True,
                    separators=(",", ":"),
                )
            ),
        ),
    ]

    restored, progress = _restored_automatic_recon(transcript)
    assert restored is None
    assert progress == set()


def test_restored_owned_cleanup_authority_is_downgraded_to_read_only() -> None:
    continuation = _cleanup_continuation(
        "handoff:owned",
        ownership="owned",
        ownership_evidence_refs=["creation:before", "creation:after"],
    )
    transcript = [
        ChatMessage(
            role="assistant",
            content="",
            tool_calls=[
                {
                    "id": "prepare-owned",
                    "type": "function",
                    "function": {"name": "assessment_prepare", "arguments": "{}"},
                }
            ],
        ),
        ChatMessage(
            role="tool",
            name="assessment_prepare",
            tool_call_id="prepare-owned",
            content=(
                "held\n[merlin-automatic-recon]"
                + json.dumps(
                    {"state": "held", "continuation": continuation},
                    sort_keys=True,
                    separators=(",", ":"),
                )
            ),
        ),
    ]

    restored, _ = _restored_automatic_recon(transcript)
    assert restored is not None
    assert restored["session_ownership"] == "unknown"
    assert restored["ownership_evidence_refs"] == []
    assert restored["allowed_tools"] == _DISCOVERY_TOOLS


@pytest.mark.asyncio
async def test_direct_owned_cleanup_invocation_without_active_continuation_is_rejected() -> None:
    class RecordingTools(Tools):
        def __init__(self) -> None:
            self.calls = []

        async def invoke(self, name, context, arguments):
            self.calls.append(name)
            return ToolResult(content="should not run")

    store = Store()
    tools = RecordingTools()
    kernel = Kernel(Provider(), store, tools, Settings())
    session_id = await kernel.start()
    await kernel._invoke_tools(  # noqa: SLF001
        session_id,
        [ToolCall(call_id="cleanup", name="browser_end_owned_session", arguments={})],
    )

    assert tools.calls == []
    assert any(
        message.role == "tool"
        and message.name == "browser_end_owned_session"
        and "not admitted" in message.content
        for message in store.messages[session_id]
    )


@pytest.mark.asyncio
async def test_provider_cannot_call_unadvertised_owned_cleanup_tool() -> None:
    class RecordingTools(Tools):
        def __init__(self) -> None:
            self.calls = []

        def describe(self):
            return (
                ToolSpec(name="observe", description="Observe", input_schema={}),
                ToolSpec(
                    name="browser_end_owned_session",
                    description="End owned session",
                    input_schema={},
                ),
            )

        async def invoke(self, name, context, arguments):
            self.calls.append(name)
            return ToolResult(content="should not run")

    class MaliciousProvider:
        def __init__(self) -> None:
            self.requests = []

        async def stream(self, request):
            self.requests.append(request)
            if len(self.requests) == 1:
                yield ToolCall(
                    call_id="forged-cleanup",
                    name="browser_end_owned_session",
                    arguments={},
                )
                yield ModelCompleted(finish_reason="tool_calls")
            else:
                yield TextDelta(text="done")
                yield ModelCompleted()

    tools = RecordingTools()
    provider = MaliciousProvider()
    kernel = Kernel(provider, Store(), tools, Settings())
    await kernel.start()
    await kernel.submit("normal turn")
    await kernel.wait_for_turn()

    assert all(
        tool["function"]["name"] != "browser_end_owned_session"
        for tool in provider.requests[0].tools
    )
    assert tools.calls == []


@pytest.mark.asyncio
async def test_dynamic_provider_cannot_call_unadvertised_owned_cleanup_tool() -> None:
    class RecordingTools(Tools):
        def __init__(self) -> None:
            self.calls = []

        def describe(self):
            return (
                ToolSpec(name="observe", description="Observe", input_schema={}),
                ToolSpec(
                    name="browser_end_owned_session",
                    description="End owned session",
                    input_schema={},
                ),
            )

        async def invoke(self, name, context, arguments):
            self.calls.append(name)
            return ToolResult(content="should not run")

    class MaliciousDynamicProvider:
        def __init__(self) -> None:
            self.requests = []
            self.result = None

        async def stream_with_tools(self, request, handler):
            self.requests.append(request)
            self.result = await handler(
                ToolCall(
                    call_id="forged-dynamic-cleanup",
                    name="browser_end_owned_session",
                    arguments={},
                )
            )
            yield TextDelta(text="done")
            yield ModelCompleted()

        async def stream(self, request):
            raise AssertionError("interactive path expected")
            yield

    tools = RecordingTools()
    provider = MaliciousDynamicProvider()
    kernel = Kernel(provider, Store(), tools, Settings())
    await kernel.start()
    await kernel.submit("normal turn")
    await kernel.wait_for_turn()

    assert all(
        tool["function"]["name"] != "browser_end_owned_session"
        for tool in provider.requests[0].tools
    )
    assert provider.result is not None and provider.result.success is False
    assert tools.calls == []


def test_command_parsing_preserves_quoted_arguments():
    assert parse_command('/model "small model"').arguments == ["small model"]
    assert parse_command("/doctor").arguments == []
    assert parse_command("/logs 25").arguments == ["25"]
    assert parse_command("/history").arguments == []
    assert parse_command("/branch 7").arguments == ["7"]
    assert parse_command("/rewind 4").arguments == ["4"]
    assert parse_command("/restore").arguments == []


@pytest.mark.asyncio
async def test_branch_rewind_and_restore_route_to_durable_session_projection() -> None:
    store = Store()
    kernel = Kernel(Provider(), store, Tools(), Settings())
    source = await kernel.start()
    store.messages[source] = [
        ChatMessage(role="user", content="one"),
        ChatMessage(role="assistant", content="two"),
        ChatMessage(role="user", content="three"),
    ]

    await kernel.submit("/rewind 2")
    assert store.projection_head == (source, 2)
    await kernel.submit("/restore")
    assert store.projection_head == (source, None)
    await kernel.submit("/branch 2")

    assert kernel.session_id == f"{source}-branch-2"
    assert store.messages[kernel.session_id] == store.messages[source][:2]
    assert any(
        event.type == "session" and event.payload.get("text") == "conversation branched"
        for event in store.events
    )


@pytest.mark.asyncio
async def test_branch_rewind_and_restore_validate_operator_arguments() -> None:
    store = Store()
    kernel = Kernel(Provider(), store, Tools(), Settings())
    await kernel.start()

    for command in ("/branch", "/branch nope", "/rewind", "/rewind -1", "/restore extra"):
        await kernel.submit(command)

    errors = [event.payload["text"] for event in store.events if event.type == "error"]
    assert errors == [
        "usage: /branch MESSAGE_SEQUENCE",
        "usage: /branch MESSAGE_SEQUENCE",
        "usage: /rewind MESSAGE_SEQUENCE",
        "usage: /rewind MESSAGE_SEQUENCE",
        "usage: /restore",
    ]


def test_operator_commands_are_recognized_but_internal_campaign_controls_are_not():
    assert parse_command('/targets add "local model" http://localhost').arguments == [
        "add",
        "local model",
        "http://localhost",
    ]
    assert parse_command("/skills enable reviewer").arguments == ["enable", "reviewer"]
    with pytest.raises(CommandError, match="unknown command"):
        parse_command("/packs inspect starter")
    with pytest.raises(CommandError, match="unknown command"):
        parse_command('/swarm start "review auth"')
    assert parse_command('/agent message a1 "focus tests"').arguments == [
        "message",
        "a1",
        "focus tests",
    ]
    assert parse_command('/report run --output "my report.md" --format markdown').arguments == [
        "run",
        "--output",
        "my report.md",
        "--format",
        "markdown",
    ]
    assert parse_command('/mcp add local --env "TOKEN=secret value" command').arguments == [
        "add",
        "local",
        "--env",
        "TOKEN=secret value",
        "command",
    ]
    assert parse_command("/mode auto").arguments == ["auto"]
    assert parse_command("/permissions").arguments == []
    assert parse_command("/memory list quarantined").arguments == ["list", "quarantined"]


class Permissions:
    def __init__(self, *, provider_allowed: bool = True):
        self.mode = "prompt"
        self.provider_allowed = provider_allowed
        self.provider_calls = []

    async def get_mode(self):
        return self.mode

    async def set_mode(self, mode, *, session_id):
        assert session_id
        self.mode = mode
        return mode

    async def list_permissions(self, session_id):
        return [{"session_id": session_id, "verdict": "allow"}]

    async def authorize_provider(self, session_id, workspace_root):
        self.provider_calls.append((session_id, workspace_root))
        return self.provider_allowed


async def test_permission_commands_and_status_are_kernel_visible() -> None:
    store = Store()
    permissions = Permissions()
    kernel = Kernel(Provider(), store, Tools(), Settings(), permissions=permissions)
    await kernel.start()

    await kernel.submit("/mode auto")
    await kernel.submit("/permissions")
    await kernel.submit("/status")

    assert permissions.mode == "auto"
    payloads = [event.payload for event in store.events]
    assert any(payload.get("command") == "mode" for payload in payloads)
    assert any(payload.get("command") == "permissions" for payload in payloads)
    assert any("permission_mode" in str(payload.get("text")) for payload in payloads)


async def test_provider_call_is_denied_before_streaming() -> None:
    class MustNotRun:
        def stream(self, request):
            raise AssertionError("provider policy was bypassed")

    store = Store()
    permissions = Permissions(provider_allowed=False)
    kernel = Kernel(MustNotRun(), store, Tools(), Settings(), permissions=permissions)
    session = await kernel.start()

    await kernel.submit("hi")
    await kernel.wait_for_turn()

    assert len(permissions.provider_calls) == 1
    assert store.messages[session] == [ChatMessage(role="user", content="hi")]
    assert store.events[-1].type == "permission"


def test_streams_and_persists_a_turn():
    asyncio.run(_streams_and_persists_a_turn())


async def _streams_and_persists_a_turn():
    store = Store()
    kernel = Kernel(Provider(), store, Tools(), Settings())
    session = await kernel.start()
    await kernel.submit("hi")
    await kernel.wait_for_turn()
    assert store.messages[session] == [
        ChatMessage(role="user", content="hi"),
        ChatMessage(role="assistant", content="hello"),
    ]
    assert [event.type for event in store.events if event.type != "warning"][-3:] == [
        "message",
        "stream",
        "message",
    ]
    # The provider double advertises no context window, so the kernel labels its
    # tier-7 conservative assumption instead of applying it silently.
    assert any(
        event.type == "warning" and event.payload.get("code") == "context_window_assumed"
        for event in store.events
    )


def _context_window_warnings(store):
    return [
        event
        for event in store.events
        if event.type == "warning" and event.payload.get("code") == "context_window_assumed"
    ]


@pytest.mark.asyncio
async def test_registry_builtin_catalog_resolves_known_model_without_warning() -> None:
    from merlin_providers import ModelCapabilityRegistry

    store = Store()
    settings = Settings()
    settings.model = "gpt-5.4-heldout-v1"
    kernel = Kernel(
        Provider(), store, Tools(), settings, capability_registry=ModelCapabilityRegistry()
    )
    await kernel.start()
    await kernel.submit("hi")
    await kernel.wait_for_turn()

    assert not _context_window_warnings(store)


@pytest.mark.asyncio
async def test_registry_operator_override_wins_over_builtin_catalog() -> None:
    from merlin_providers import ModelCapabilityRegistry, OperatorOverride, RouteIdentity

    registry = ModelCapabilityRegistry(
        overrides=[
            OperatorOverride(
                route=RouteIdentity(transport="openai_compatible", endpoint="", model="acme-x"),
                values={"context_window_tokens": 33_000},
            )
        ]
    )
    kernel = Kernel(Provider(), Store(), Tools(), Settings(), capability_registry=registry)

    assert await kernel._context_window_tokens(None, model="acme-x") == 33_000  # noqa: SLF001


@pytest.mark.asyncio
async def test_registry_tier_seven_still_warns_for_unknown_model() -> None:
    from merlin_providers import ModelCapabilityRegistry

    store = Store()
    kernel = Kernel(
        Provider(), store, Tools(), Settings(), capability_registry=ModelCapabilityRegistry()
    )
    await kernel.start()
    await kernel.submit("hi")
    await kernel.wait_for_turn()

    # The registry itself fell through to tier 7 for 'test-model', so the
    # labelled-assumption warning must fire exactly as without a registry.
    assert _context_window_warnings(store)


@pytest.mark.asyncio
async def test_registry_resolves_per_route_identity_from_provider() -> None:
    from merlin_providers import ModelCapabilityRegistry, OperatorOverride, RouteIdentity

    class RoutedProvider(Provider):
        def route_identity_for_model(self, model):
            if model == "acme-x":
                return RouteIdentity(transport="claude_cli", endpoint="", model=model)
            return None

    registry = ModelCapabilityRegistry(
        overrides=[
            OperatorOverride(
                route=RouteIdentity(transport="claude_cli", endpoint="", model="acme-x"),
                values={"context_window_tokens": 22_000},
            )
        ]
    )
    kernel = Kernel(RoutedProvider(), Store(), Tools(), Settings(), capability_registry=registry)

    assert await kernel._context_window_tokens(None, model="acme-x") == 22_000  # noqa: SLF001


@pytest.mark.asyncio
async def test_unknown_model_on_native_transport_still_warns() -> None:
    from merlin_providers import ModelCapabilityRegistry, RouteIdentity

    class RoutedProvider(Provider):
        def route_identity_for_model(self, model):
            return RouteIdentity(
                transport="openai_responses", endpoint="https://api.example/v1", model=model
            )

    store = Store()
    kernel = Kernel(
        RoutedProvider(), store, Tools(), Settings(), capability_registry=ModelCapabilityRegistry()
    )
    await kernel.start()
    await kernel.submit("hi")
    await kernel.wait_for_turn()

    # A native-transport route for an unknown model still falls through to the
    # labelled tier-7 assumption; route identity never silences the warning.
    assert _context_window_warnings(store)


@pytest.mark.asyncio
async def test_parallel_tool_calls_are_one_assistant_message_with_reasoning() -> None:
    class ParallelProvider:
        def __init__(self):
            self.requests = []

        async def stream(self, request):
            self.requests.append(request)
            if len(self.requests) == 1:
                yield ReasoningDelta(text="inspect both")
                yield TextDelta(text="I will inspect both.")
                yield ToolCall(call_id="call-a", name="read", arguments={"path": "a"})
                yield ToolCall(call_id="call-b", name="read", arguments={"path": "b"})
            else:
                yield TextDelta(text="done")
                yield ModelCompleted()

    class ParallelTools(Tools):
        def describe(self):
            return (
                ToolSpec(
                    name="read",
                    description="read",
                    input_schema={"type": "object"},
                ),
            )

        async def invoke(self, name, context, arguments):
            if arguments["path"] == "b":
                raise RuntimeError("unavailable")
            return ToolResult(content="read a")

    store = Store()
    provider = ParallelProvider()
    kernel = Kernel(provider, store, ParallelTools(), Settings())
    session = await kernel.start()
    await kernel.submit("inspect")
    await kernel.wait_for_turn()

    assistant_call = store.messages[session][1]
    assert assistant_call.reasoning_content == "inspect both"
    assert assistant_call.content == "I will inspect both."
    assert [call["id"] for call in assistant_call.tool_calls] == ["call-a", "call-b"]
    assert [message.tool_call_id for message in store.messages[session][2:4]] == [
        "call-a",
        "call-b",
    ]
    assert store.messages[session][2].content == "read a"
    assert store.messages[session][3].content == "read failed: unavailable"
    assert provider.requests[1].messages[-3:] == store.messages[session][1:4]


@pytest.mark.asyncio
async def test_tool_call_thought_signature_survives_provider_recreation() -> None:
    class SignatureProvider:
        def __init__(self, signatures=None):
            self.requests = []
            self._signatures = dict(signatures or {})

        @property
        def tool_call_signatures(self):
            return dict(self._signatures)

        async def stream(self, request):
            self.requests.append(request)
            if len(self.requests) == 1:
                yield ToolCall(call_id="call-a", name="read", arguments={"path": "a"})
            else:
                yield TextDelta(text="done")
                yield ModelCompleted()

    class ReadTools(Tools):
        def describe(self):
            return (
                ToolSpec(
                    name="read",
                    description="read",
                    input_schema={"type": "object"},
                ),
            )

        async def invoke(self, name, context, arguments):
            return ToolResult(content="read a")

    store = Store()
    kernel = Kernel(SignatureProvider({"call-a": "sig-abc"}), store, ReadTools(), Settings())
    session = await kernel.start()
    await kernel.submit("inspect")
    await kernel.wait_for_turn()

    assistant_call = store.messages[session][1]
    assert assistant_call.tool_calls[0]["thought_signature"] == "sig-abc"

    # Daemon restart: a fresh provider has no in-memory signatures; the replayed
    # transcript must carry the persisted thought_signature instead.
    restarted = Kernel(SignatureProvider(), store, ReadTools(), Settings())
    await restarted.start(session)
    await restarted.submit("continue")
    await restarted.wait_for_turn()

    replayed = restarted._provider.requests[0].messages  # noqa: SLF001
    replayed_call = next(m for m in replayed if m.tool_calls).tool_calls[0]
    assert replayed_call["thought_signature"] == "sig-abc"


@pytest.mark.asyncio
async def test_interruption_closes_active_and_pending_tool_calls() -> None:
    started = asyncio.Event()

    class ToolProvider:
        async def stream(self, request):
            yield ToolCall(call_id="call-a", name="read", arguments={"path": "a"})
            yield ToolCall(call_id="call-b", name="read", arguments={"path": "b"})

    class BlockingTools(Tools):
        def describe(self):
            return (ToolSpec(name="read", description="read", input_schema={"type": "object"}),)

        async def invoke(self, name, context, arguments):
            started.set()
            await asyncio.Event().wait()

    store = Store()
    kernel = Kernel(ToolProvider(), store, BlockingTools(), Settings())
    session = await kernel.start()
    await kernel.submit("inspect")
    await started.wait()

    assert await kernel.interrupt()

    tool_results = [message for message in store.messages[session] if message.role == "tool"]
    assert [message.tool_call_id for message in tool_results] == ["call-a", "call-b"]
    assert all("interrupted" in message.content for message in tool_results)


@pytest.mark.asyncio
async def test_interruption_closes_dynamic_tool_call() -> None:
    started = asyncio.Event()

    class DynamicProvider:
        async def stream_with_tools(self, request, handler):
            await handler(ToolCall(call_id="dynamic-1", name="read", arguments={"path": "a"}))
            yield ModelCompleted()

    class BlockingTools(Tools):
        def describe(self):
            return (ToolSpec(name="read", description="read", input_schema={"type": "object"}),)

        async def invoke(self, name, context, arguments):
            started.set()
            await asyncio.Event().wait()

    store = Store()
    kernel = Kernel(DynamicProvider(), store, BlockingTools(), Settings())
    session = await kernel.start()
    await kernel.submit("inspect")
    await started.wait()

    assert await kernel.interrupt()

    tool_results = [message for message in store.messages[session] if message.role == "tool"]
    assert len(tool_results) == 1
    assert tool_results[0].tool_call_id == "dynamic-1"
    assert "interrupted" in tool_results[0].content


@pytest.mark.asyncio
async def test_restart_closes_orphaned_tool_call_once_before_provider_replay() -> None:
    class CapturingProvider:
        def __init__(self):
            self.requests = []

        async def stream(self, request):
            self.requests.append(request)
            yield TextDelta(text="done")
            yield ModelCompleted()

    store, provider = Store(), CapturingProvider()
    session = await store.create_session()
    store.messages[session].append(
        ChatMessage(
            role="assistant",
            content="",
            tool_calls=[
                {
                    "id": "orphan-1",
                    "type": "function",
                    "function": {"name": "read", "arguments": "{}"},
                }
            ],
        )
    )
    kernel = Kernel(provider, store, Tools(), Settings())
    await kernel.start(session)

    await kernel.submit("resume")
    await kernel.wait_for_turn()
    await kernel.submit("continue")
    await kernel.wait_for_turn()

    terminal = [
        message
        for message in store.messages[session]
        if message.role == "tool" and message.tool_call_id == "orphan-1"
    ]
    assert len(terminal) == 1
    assert "process restart" in terminal[0].content
    assert any(message.tool_call_id == "orphan-1" for message in provider.requests[0].messages)


def test_resume_and_interrupt():
    asyncio.run(_resume_and_interrupt())


async def _resume_and_interrupt():
    class SlowProvider:
        async def stream(self, request):
            yield TextDelta(text="partial")
            await asyncio.Event().wait()

    store = Store()
    existing = await store.create_session()
    kernel = Kernel(SlowProvider(), store, Tools(), Settings())
    await kernel.start(existing)
    await kernel.submit("hi")
    await asyncio.sleep(0)
    assert await kernel.interrupt()
    assert store.messages[existing][-1] == ChatMessage(role="assistant", content="partial")


@pytest.mark.asyncio
async def test_partial_provider_error_is_durable_and_restored_on_replacement() -> None:
    class PartialProvider:
        async def stream(self, request):
            yield ReasoningDelta(text="recoverable reasoning")
            yield TextDelta(text="recoverable partial")
            yield ModelError(
                message="provider stream ended before completion",
                code="incomplete_stream",
                partial=False,
            )

    class ReplacementProvider:
        def __init__(self):
            self.requests = []

        async def stream(self, request):
            self.requests.append(request)
            yield TextDelta(text="continued")
            yield ModelCompleted()

    store = Store()
    session = await store.create_session()
    first = Kernel(PartialProvider(), store, Tools(), Settings())
    await first.start(session)
    await first.submit("begin")
    await first.wait_for_turn()

    partial = store.messages[session][-1]
    assert partial == ChatMessage(
        role="assistant",
        content="recoverable partial",
        reasoning_content="recoverable reasoning",
    )

    replacement = ReplacementProvider()
    resumed = Kernel(replacement, store, Tools(), Settings())
    await resumed.start(session)
    await resumed.submit("continue")
    await resumed.wait_for_turn()

    assert partial in replacement.requests[0].messages


@pytest.mark.asyncio
async def test_retryable_malformed_tool_call_is_repaired_inside_interactive_turn() -> None:
    class RepairingProvider:
        def __init__(self):
            self.requests = []

        async def stream(self, request):
            self.requests.append(request)
            if len(self.requests) == 1:
                yield ReasoningDelta(text="attempting a tool call")
                yield ModelError(
                    message="provider returned malformed tool arguments",
                    code="malformed_tool_arguments",
                    retryable=True,
                )
                return
            yield TextDelta(text="Recovered")
            yield ModelCompleted()

    provider = RepairingProvider()
    store = Store()
    kernel = Kernel(provider, store, Tools(), Settings(), max_provider_retries=1)
    await kernel.start()
    await kernel.submit("inspect")
    await kernel.wait_for_turn()

    assert len(provider.requests) == 2
    assert "Emit exactly one complete JSON object" in provider.requests[1].messages[-2].content
    assert provider.requests[1].reasoning == "disabled"
    assert store.messages[kernel.session_id][-1].content == "Recovered"


@pytest.mark.asyncio
async def test_truncated_tool_call_retries_exact_tool_without_reasoning() -> None:
    class AvailableTools(Tools):
        def describe(self):
            return (
                ToolSpec(name="shell_command", description="Run", input_schema={}),
                ToolSpec(name="read_file", description="Read", input_schema={}),
            )

    class RepairingProvider:
        def __init__(self):
            self.requests = []

        async def stream(self, request):
            self.requests.append(request)
            if len(self.requests) == 1:
                yield ReasoningDelta(text="long reconnaissance reasoning")
                yield ModelError(
                    message="provider truncated tool arguments at its output limit",
                    code="truncated_tool_call",
                    retryable=True,
                    partial=True,
                    tool_name="shell_command",
                    failure_kind=ProviderFailureKind.TRUNCATED,
                )
                return
            if len(self.requests) == 2:
                yield ToolCall(
                    call_id="repaired-shell",
                    name="shell_command",
                    arguments={"argv": ["python3", "-c", "print('ok')"]},
                )
                yield ModelCompleted(finish_reason="tool_calls")
                return
            yield TextDelta(text="Session bootstrap completed.")
            yield ModelCompleted()

    class RecordingTools(AvailableTools):
        def __init__(self):
            self.calls = []

        async def invoke(self, name, context, arguments):
            self.calls.append((name, arguments))
            return ToolResult(content="ok")

    provider = RepairingProvider()
    tools = RecordingTools()
    store = Store()
    kernel = Kernel(provider, store, tools, Settings(), max_provider_retries=1)
    await kernel.start()
    await kernel.submit("inspect")
    await kernel.wait_for_turn()

    assert len(provider.requests) == 3
    assert provider.requests[1].reasoning == "disabled"
    assert [tool["function"]["name"] for tool in provider.requests[1].tools] == ["shell_command"]
    assert tools.calls == [("shell_command", {"argv": ["python3", "-c", "print('ok')"]})]


@pytest.mark.asyncio
async def test_reasoning_only_completion_is_retried_inside_the_same_turn() -> None:
    class ReasoningOnlyThenAnswerProvider:
        def __init__(self):
            self.requests = []

        async def stream(self, request):
            self.requests.append(request)
            if len(self.requests) == 1:
                yield ReasoningDelta(text="I should inspect the target.")
                yield ModelCompleted()
                return
            yield TextDelta(text="Recovered with a visible response.")
            yield ModelCompleted()

    provider = ReasoningOnlyThenAnswerProvider()
    store = Store()
    kernel = Kernel(provider, store, Tools(), Settings(), max_provider_retries=1)
    await kernel.start()
    await kernel.submit("inspect")
    await kernel.wait_for_turn()

    assert len(provider.requests) == 2
    assert "neither user-visible content nor a tool call" in (
        provider.requests[1].messages[-2].content
    )
    assert store.messages[kernel.session_id][-1] == ChatMessage(
        role="assistant", content="Recovered with a visible response."
    )
    warnings = [event for event in store.events if event.type == "warning"]
    assert any(event.payload.get("code") == "reasoning_only_retry" for event in warnings)


@pytest.mark.asyncio
async def test_repeated_reasoning_only_completion_fails_with_typed_error() -> None:
    class ReasoningOnlyProvider:
        def __init__(self):
            self.requests = []

        async def stream(self, request):
            self.requests.append(request)
            yield ReasoningDelta(text="Still thinking.")
            yield ModelCompleted()

    provider = ReasoningOnlyProvider()
    store = Store()
    kernel = Kernel(provider, store, Tools(), Settings(), max_provider_retries=2)
    await kernel.start()
    await kernel.submit("inspect")
    await kernel.wait_for_turn()

    assert len(provider.requests) == 3
    assert not [
        message for message in store.messages[kernel.session_id] if message.role == "assistant"
    ]
    errors = [event for event in store.events if event.type == "error"]
    assert errors[-1].payload["code"] == "empty_provider_response"


@pytest.mark.asyncio
async def test_interactive_dynamic_tool_provider_uses_merlin_broker_and_continues() -> None:
    class DynamicProvider:
        async def stream(self, request):
            raise AssertionError("dynamic provider must use stream_with_tools")
            yield  # pragma: no cover

        async def stream_with_tools(self, request, handler):
            result = await handler(
                ToolCall(call_id="dynamic-1", name="read_file", arguments={"path": "README.md"})
            )
            assert result == DynamicToolOutput(content="contents", success=True)
            yield TextDelta(text="Reconnaissance continued.")
            yield ModelCompleted()

    class DynamicTools(Tools):
        def __init__(self):
            self.calls = []

        def describe(self):
            return (ToolSpec(name="read_file", description="Read", input_schema={}),)

        async def invoke(self, name, context, arguments):
            self.calls.append((name, context.call_id, arguments))
            return ToolResult(content="contents")

    tools = DynamicTools()
    store = Store()
    kernel = Kernel(DynamicProvider(), store, tools, Settings())
    await kernel.start()
    await kernel.submit("inspect")
    await kernel.wait_for_turn()

    assert tools.calls == [("read_file", "dynamic-1", {"path": "README.md"})]
    assert [message.role for message in store.messages[kernel.session_id]] == [
        "user",
        "assistant",
        "tool",
        "assistant",
    ]
    assert store.messages[kernel.session_id][-1].content == "Reconnaissance continued."


@pytest.mark.asyncio
async def test_immediate_action_commitment_continues_until_tool_is_called() -> None:
    class FollowThroughProvider:
        def __init__(self):
            self.requests = []

        async def stream(self, request):
            self.requests.append(request)
            if len(self.requests) == 1:
                yield TextDelta(text="I'm continuing with the API lifecycle inspection now.")
                yield ModelCompleted()
                return
            if len(self.requests) == 2:
                yield ToolCall(
                    call_id="inspect-1",
                    name="browser_snapshot",
                    arguments={"tab_ref": "tab:1"},
                )
                yield ModelCompleted()
                return
            yield TextDelta(text="Inspection captured.")
            yield ModelCompleted()

    class BrowserTools(Tools):
        def __init__(self):
            self.calls = []

        def describe(self):
            return (ToolSpec(name="browser_snapshot", description="Snapshot", input_schema={}),)

        async def invoke(self, name, context, arguments):
            self.calls.append((name, context.call_id, arguments))
            return ToolResult(content="captured")

    provider = FollowThroughProvider()
    tools = BrowserTools()
    store = Store()
    kernel = Kernel(provider, store, tools, Settings())
    await kernel.start()
    await kernel.submit("Run the already-authorized reconnaissance")
    await kernel.wait_for_turn()

    assert len(provider.requests) == 3
    assert tools.calls == [("browser_snapshot", "inspect-1", {"tab_ref": "tab:1"})]
    assert any(
        "committed to an immediate next action" in message.content
        for message in provider.requests[1].messages
    )


@pytest.mark.asyncio
async def test_immediate_action_commitment_recovery_is_bounded() -> None:
    class NeverActsProvider:
        def __init__(self):
            self.requests = []

        async def stream(self, request):
            self.requests.append(request)
            yield TextDelta(text="I'll continue by inspecting it now.")
            yield ModelCompleted()

    class BrowserTools(Tools):
        def describe(self):
            return (ToolSpec(name="browser_snapshot", description="Snapshot", input_schema={}),)

    provider = NeverActsProvider()
    store = Store()
    kernel = Kernel(provider, store, BrowserTools(), Settings())
    await kernel.start()
    await kernel.submit("inspect")
    await kernel.wait_for_turn()

    assert len(provider.requests) == 3


@pytest.mark.asyncio
async def test_jev_second_opinion_recovers_missed_commitment(monkeypatch) -> None:
    class FollowThroughProvider:
        def __init__(self):
            self.requests = []

        async def stream(self, request):
            self.requests.append(request)
            if len(self.requests) == 1:
                yield TextDelta(text="Capturing the current page state next.")
                yield ModelCompleted()
                return
            if len(self.requests) == 2:
                yield ToolCall(
                    call_id="inspect-1",
                    name="browser_snapshot",
                    arguments={"tab_ref": "tab:1"},
                )
                yield ModelCompleted()
                return
            yield TextDelta(text="Inspection captured.")
            yield ModelCompleted()

    class BrowserTools(Tools):
        def __init__(self):
            self.calls = []

        def describe(self):
            return (ToolSpec(name="browser_snapshot", description="Snapshot", input_schema={}),)

        async def invoke(self, name, context, arguments):
            self.calls.append((name, context.call_id, arguments))
            return ToolResult(content="captured")

    def fake_jev_post(url, token, payload, timeout_seconds):
        probability = 0.9 if "Capturing the current page state" in str(payload["state"]) else 0.1
        return {
            "answers": {"question": {"noul": probability}},
            "usage": {"input_tokens": 12, "output_tokens": 3},
        }

    monkeypatch.setattr("merlin_kernel.kernel._jev_post", fake_jev_post)
    provider = FollowThroughProvider()
    tools = BrowserTools()
    store = Store()
    kernel = Kernel(provider, store, tools, Settings())
    kernel._jev_client = _KernelJevClient(token="test-token")
    kernel._jev_client_checked = True
    await kernel.start()
    await kernel.submit("inspect")
    await kernel.wait_for_turn()

    assert len(provider.requests) == 3
    assert tools.calls == [("browser_snapshot", "inspect-1", {"tab_ref": "tab:1"})]
    assert any(
        "committed to an immediate next action" in message.content
        for message in provider.requests[1].messages
    )
    judgments = [event for event in store.events if event.type == "judgment"]
    assert judgments[0].payload["verdict"] == "committed"
    assert judgments[0].payload["graded_by"] == "jev"
    assert judgments[0].payload["input_tokens"] == 12
    assert judgments[0].payload["output_tokens"] == 3


@pytest.mark.asyncio
async def test_jev_second_opinion_below_threshold_keeps_phrase_result(monkeypatch) -> None:
    class QuietProvider:
        def __init__(self):
            self.requests = []

        async def stream(self, request):
            self.requests.append(request)
            yield TextDelta(text="Capturing the current page state next.")
            yield ModelCompleted()

    class BrowserTools(Tools):
        def describe(self):
            return (ToolSpec(name="browser_snapshot", description="Snapshot", input_schema={}),)

    def fake_jev_post(url, token, payload, timeout_seconds):
        return {"answers": {"question": {"noul": 0.4}}}

    monkeypatch.setattr("merlin_kernel.kernel._jev_post", fake_jev_post)
    provider = QuietProvider()
    store = Store()
    kernel = Kernel(provider, store, BrowserTools(), Settings())
    kernel._jev_client = _KernelJevClient(token="test-token")
    kernel._jev_client_checked = True
    await kernel.start()
    await kernel.submit("inspect")
    await kernel.wait_for_turn()

    assert len(provider.requests) == 1


@pytest.mark.asyncio
async def test_jev_failure_keeps_phrase_list_result(monkeypatch) -> None:
    class QuietProvider:
        def __init__(self):
            self.requests = []

        async def stream(self, request):
            self.requests.append(request)
            yield TextDelta(text="Capturing the current page state next.")
            yield ModelCompleted()

    class BrowserTools(Tools):
        def describe(self):
            return (ToolSpec(name="browser_snapshot", description="Snapshot", input_schema={}),)

    def failing_jev_post(url, token, payload, timeout_seconds):
        raise OSError("network unreachable")

    monkeypatch.setattr("merlin_kernel.kernel._jev_post", failing_jev_post)
    provider = QuietProvider()
    store = Store()
    kernel = Kernel(provider, store, BrowserTools(), Settings())
    kernel._jev_client = _KernelJevClient(token="test-token")
    kernel._jev_client_checked = True
    await kernel.start()
    await kernel.submit("inspect")
    await kernel.wait_for_turn()

    assert len(provider.requests) == 1
    assert not any(event.type == "judgment" for event in store.events)


@pytest.mark.asyncio
async def test_tool_turn_continues_beyond_twelve_rounds_until_provider_completes() -> None:
    class LoopingProvider:
        def __init__(self):
            self.requests = []

        async def stream(self, request):
            self.requests.append(request)
            if len(self.requests) <= 16:
                yield ToolCall(
                    call_id=f"inspect-{len(self.requests)}",
                    name="browser_snapshot",
                    arguments={"tab_ref": "tab:1"},
                )
            else:
                yield TextDelta(text="Reconnaissance and preparation completed.")
            yield ModelCompleted()

    class BrowserTools(Tools):
        def __init__(self):
            self.calls = []

        def describe(self):
            return (ToolSpec(name="browser_snapshot", description="Snapshot", input_schema={}),)

        async def invoke(self, name, context, arguments):
            self.calls.append((name, context.call_id, arguments))
            return ToolResult(content="captured")

    provider = LoopingProvider()
    tools = BrowserTools()
    store = Store()
    kernel = Kernel(provider, store, tools, Settings())
    await kernel.start()
    await kernel.submit("inspect")
    await kernel.wait_for_turn()

    assert len(provider.requests) == 17
    assert len(tools.calls) == 16
    assert provider.requests[-1].tools
    assert store.messages[kernel.session_id][-1].content == (
        "Reconnaissance and preparation completed."
    )
    assert not any(event.payload.get("code") == "tool_round_limit" for event in store.events)


@pytest.mark.asyncio
async def test_retryable_transport_timeout_resumes_after_durable_dynamic_tool_result() -> None:
    class RecoveringProvider:
        def __init__(self):
            self.requests = []

        async def stream(self, request):
            raise AssertionError("dynamic provider must use stream_with_tools")
            yield  # pragma: no cover

        async def stream_with_tools(self, request, handler):
            self.requests.append(request)
            if len(self.requests) == 1:
                result = await handler(
                    ToolCall(
                        call_id="snapshot-1",
                        name="browser_snapshot",
                        arguments={"tab_ref": "tab:1"},
                    )
                )
                assert result.success is True
                yield ModelError(
                    message="Codex App Server emitted no protocol message for 300 seconds",
                    code="codex_timeout",
                    retryable=True,
                )
                return
            assert any(
                message.role == "tool" and message.tool_call_id == "snapshot-1"
                for message in request.messages
            )
            assert [tool["function"]["name"] for tool in request.tools] == ["browser_snapshot"]
            yield TextDelta(text="Reconnaissance resumed after the stored snapshot.")
            yield ModelCompleted()

    class BrowserTools(Tools):
        def __init__(self):
            self.calls = []

        def describe(self):
            return (ToolSpec(name="browser_snapshot", description="Snapshot", input_schema={}),)

        async def invoke(self, name, context, arguments):
            self.calls.append((name, arguments))
            return ToolResult(content="durable snapshot")

    provider = RecoveringProvider()
    tools = BrowserTools()
    store = Store()
    kernel = Kernel(provider, store, tools, Settings(), max_provider_retries=1)
    await kernel.start()
    await kernel.submit("recon")
    await kernel.wait_for_turn()

    assert tools.calls == [("browser_snapshot", {"tab_ref": "tab:1"})]
    assert len(provider.requests) == 2
    assert [event.payload.get("code") for event in store.events].count("provider_retry") == 1
    assert store.messages[kernel.session_id][-1].content.startswith("Reconnaissance resumed")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "code",
    [
        "connect_timeout",
        "read_timeout",
        "write_timeout",
        "pool_timeout",
        "timeout",
        "total_timeout",
        "transport_error",
        "rate_limited",
        "incomplete_stream",
    ],
)
async def test_provider_owned_transport_failure_is_not_retried_by_kernel(code: str) -> None:
    class ExhaustedProvider:
        def __init__(self):
            self.requests = []

        async def stream(self, request):
            self.requests.append(request)
            yield ModelError(message=f"provider {code}", code=code, retryable=True)

    provider = ExhaustedProvider()
    store = Store()
    kernel = Kernel(provider, store, Tools(), Settings(), max_provider_retries=3)
    await kernel.start()
    await kernel.submit("inspect")
    await kernel.wait_for_turn()

    assert len(provider.requests) == 1
    assert not any(event.payload.get("code") == "provider_retry" for event in store.events)
    assert [event.payload.get("code") for event in store.events].count(code) == 1


@pytest.mark.asyncio
async def test_partial_provider_owned_transport_failure_is_retried_by_kernel() -> None:
    class PartialProvider:
        def __init__(self):
            self.requests = []

        async def stream(self, request):
            self.requests.append(request)
            if len(self.requests) == 1:
                yield TextDelta(text="partial")
                yield ModelError(
                    message="provider read timeout",
                    code="read_timeout",
                    retryable=True,
                    partial=True,
                )
                return
            yield TextDelta(text=" recovered")
            yield ModelCompleted()

    provider = PartialProvider()
    store = Store()
    kernel = Kernel(provider, store, Tools(), Settings(), max_provider_retries=1)
    await kernel.start()
    await kernel.submit("inspect")
    await kernel.wait_for_turn()

    assert len(provider.requests) == 2
    assert any(event.payload.get("code") == "provider_retry" for event in store.events)


@pytest.mark.asyncio
async def test_repeated_malformed_tool_calls_switch_to_simpler_recovery_strategy() -> None:
    class AvailableTools(Tools):
        def describe(self):
            return (
                ToolSpec(name="read_file", description="Read", input_schema={}),
                ToolSpec(name="shell_command", description="Run", input_schema={}),
            )

    class PersistentlyMalformedProvider:
        def __init__(self):
            self.requests = []

        async def stream(self, request):
            self.requests.append(request)
            if len(self.requests) < 3:
                yield ModelError(
                    message="provider returned malformed tool arguments",
                    code="malformed_tool_arguments",
                    retryable=True,
                    tool_name="shell_command",
                )
                return
            yield TextDelta(text="Recovered without repeating the failed tool")
            yield ModelCompleted()

    provider = PersistentlyMalformedProvider()
    store = Store()
    kernel = Kernel(provider, store, AvailableTools(), Settings(), max_provider_retries=3)
    await kernel.start()
    await kernel.submit("inspect")
    await kernel.wait_for_turn()

    assert len(provider.requests) == 3
    assert [tool["function"]["name"] for tool in provider.requests[1].tools] == ["shell_command"]
    assert [tool["function"]["name"] for tool in provider.requests[2].tools] == [
        "read_file",
    ]
    assert "repeatedly malformed tool is unavailable" in provider.requests[2].messages[-2].content
    assert store.messages[kernel.session_id][-1].content.startswith("Recovered")


@pytest.mark.asyncio
async def test_persistent_malformed_tool_calls_never_remove_the_tool_schema() -> None:
    class AvailableTools(Tools):
        def describe(self):
            return (
                ToolSpec(
                    name="read_file",
                    description="Read a file",
                    input_schema={"type": "object"},
                ),
            )

    class PersistentlyMalformed:
        def __init__(self):
            self.requests = []

        async def stream(self, request):
            self.requests.append(request)
            yield ModelError(
                message="provider returned malformed tool arguments",
                code="malformed_tool_call",
                retryable=True,
            )

    provider = PersistentlyMalformed()
    store = Store()
    kernel = Kernel(provider, store, AvailableTools(), Settings(), max_provider_retries=3)
    await kernel.start()
    await kernel.submit("inspect")
    await kernel.wait_for_turn()

    assert len(provider.requests) == 4
    assert all(request.tools for request in provider.requests)
    assert [message.role for message in store.messages[kernel.session_id]] == ["user"]


@pytest.mark.asyncio
async def test_final_recovery_keeps_failed_tool_available() -> None:
    class RecordingTools(Tools):
        def __init__(self):
            self.calls = []

        def describe(self):
            return (
                ToolSpec(
                    name="browser_snapshot",
                    description="Snapshot",
                    input_schema={"type": "object"},
                ),
            )

        async def invoke(self, name, context, arguments):
            self.calls.append((name, arguments))
            return ToolResult(content="ok")

    class RecordingProvider:
        def __init__(self):
            self.requests = []

        async def stream(self, request):
            self.requests.append(request)
            if len(self.requests) <= 3:
                yield ModelError(
                    message="malformed",
                    code="malformed_tool_call",
                    retryable=True,
                    tool_name="browser_snapshot",
                )
                return
            if len(self.requests) == 4:
                yield ToolCall(
                    call_id="dsml-recovered",
                    name="browser_snapshot",
                    arguments={"context_id": "ctx", "tab_ref": "tab:1"},
                )
                yield ModelCompleted(finish_reason="tool_calls")
                return
            yield TextDelta(text="Reconnaissance continued.")
            yield ModelCompleted()

    provider = RecordingProvider()
    tools = RecordingTools()
    store = Store()
    kernel = Kernel(provider, store, tools, Settings(), max_provider_retries=3)
    await kernel.start()
    await kernel.submit("recon")
    await kernel.wait_for_turn()

    assert len(provider.requests) == 5
    assert [tool["function"]["name"] for tool in provider.requests[1].tools] == ["browser_snapshot"]
    assert not provider.requests[2].tools
    assert not provider.requests[3].tools
    assert "Tools are disabled" not in provider.requests[3].messages[-2].content
    assert tools.calls == [("browser_snapshot", {"context_id": "ctx", "tab_ref": "tab:1"})]
    assert store.messages[kernel.session_id][-1].content == "Reconnaissance continued."


@pytest.mark.asyncio
async def test_malformed_tool_retry_exposes_only_the_failed_tool_schema() -> None:
    class AvailableTools(Tools):
        def describe(self):
            return (
                ToolSpec(name="assessment_prepare", description="Prepare", input_schema={}),
                ToolSpec(name="shell_command", description="Run", input_schema={}),
            )

    class RepairingProvider:
        def __init__(self):
            self.requests = []

        async def stream(self, request):
            self.requests.append(request)
            if len(self.requests) == 1:
                yield ModelError(
                    message="malformed",
                    code="malformed_tool_call",
                    retryable=True,
                    tool_name="assessment_prepare",
                )
                return
            yield TextDelta(text="Recovered")
            yield ModelCompleted()

    provider = RepairingProvider()
    store = Store()
    kernel = Kernel(provider, store, AvailableTools(), Settings(), max_provider_retries=2)
    await kernel.start()
    await kernel.submit("prepare")
    await kernel.wait_for_turn()

    assert [tool["function"]["name"] for tool in provider.requests[0].tools] == [
        "assessment_prepare",
        "shell_command",
    ]
    assert [tool["function"]["name"] for tool in provider.requests[1].tools] == [
        "assessment_prepare"
    ]
    assert "Call only `assessment_prepare`" in provider.requests[1].messages[-2].content


@pytest.mark.asyncio
async def test_successful_malformed_tool_repair_restores_all_tools_next_request() -> None:
    class AvailableTools(Tools):
        def __init__(self):
            self.calls = []

        def describe(self):
            return (
                ToolSpec(name="assessment_prepare", description="Prepare", input_schema={}),
                ToolSpec(name="shell_command", description="Run", input_schema={}),
                ToolSpec(name="browser_snapshot", description="Snapshot", input_schema={}),
            )

        async def invoke(self, name, context, arguments):
            self.calls.append((name, arguments))
            return ToolResult(content="ok")

    class RepairingProvider:
        def __init__(self):
            self.requests = []

        async def stream(self, request):
            self.requests.append(request)
            if len(self.requests) == 1:
                yield ModelError(
                    message="malformed",
                    code="malformed_tool_call",
                    retryable=True,
                    tool_name="shell_command",
                )
                return
            if len(self.requests) == 2:
                yield ToolCall(
                    call_id="recovered-shell",
                    name="shell_command",
                    arguments={"cmd": "true"},
                )
                yield ModelCompleted(finish_reason="tool_calls")
                return
            if len(self.requests) == 3:
                yield ToolCall(
                    call_id="prepare-after-repair",
                    name="assessment_prepare",
                    arguments={"manifest": "fixture"},
                )
                yield ModelCompleted(finish_reason="tool_calls")
                return
            yield TextDelta(text="Prepared")
            yield ModelCompleted()

    provider = RepairingProvider()
    tools = AvailableTools()
    store = Store()
    kernel = Kernel(provider, store, tools, Settings(), max_provider_retries=2)
    await kernel.start()
    await kernel.submit("prepare")
    await kernel.wait_for_turn()

    assert [tool["function"]["name"] for tool in provider.requests[1].tools] == ["shell_command"]
    assert [tool["function"]["name"] for tool in provider.requests[2].tools] == [
        "assessment_prepare",
        "shell_command",
        "browser_snapshot",
    ]
    assert all(
        "Call only `shell_command`" not in message.content
        for message in provider.requests[2].messages
    )
    assert tools.calls == [
        ("shell_command", {"cmd": "true"}),
        ("assessment_prepare", {"manifest": "fixture"}),
    ]


@pytest.mark.asyncio
async def test_successful_tool_can_constrain_exactly_the_next_tool_request() -> None:
    class HandoffTools(Tools):
        def __init__(self):
            self.calls = []

        def describe(self):
            return (
                ToolSpec(name="observe", description="Observe", input_schema={}),
                ToolSpec(name="compile", description="Compile", input_schema={}),
                ToolSpec(name="unrelated", description="Unrelated", input_schema={}),
            )

        async def invoke(self, name, context, arguments):
            self.calls.append(name)
            if name == "observe":
                return ToolResult(
                    content="sufficient evidence",
                    metadata={"preferred_next_tool": "compile"},
                )
            return ToolResult(content="compiled")

    class HandoffProvider:
        def __init__(self):
            self.requests = []

        async def stream(self, request):
            self.requests.append(request)
            if len(self.requests) == 1:
                yield ToolCall(call_id="observe-1", name="observe", arguments={})
                yield ModelCompleted(finish_reason="tool_calls")
                return
            if len(self.requests) == 2:
                yield ToolCall(call_id="compile-1", name="compile", arguments={})
                yield ModelCompleted(finish_reason="tool_calls")
                return
            yield TextDelta(text="done")
            yield ModelCompleted()

    provider = HandoffProvider()
    tools = HandoffTools()
    kernel = Kernel(provider, Store(), tools, Settings())
    await kernel.start()
    await kernel.submit("recon")
    await kernel.wait_for_turn()

    assert [tool["function"]["name"] for tool in provider.requests[0].tools] == [
        "observe",
        "compile",
        "unrelated",
    ]
    assert [tool["function"]["name"] for tool in provider.requests[1].tools] == ["compile"]
    assert "Do not perform more reconnaissance" in provider.requests[1].messages[-1].content
    assert [tool["function"]["name"] for tool in provider.requests[2].tools] == [
        "observe",
        "compile",
        "unrelated",
    ]
    assert tools.calls == ["observe", "compile"]


@pytest.mark.asyncio
async def test_preferred_tool_refusal_is_uncertain_then_cooperates() -> None:
    class FollowThroughTools(Tools):
        def __init__(self):
            self.calls = []

        def describe(self):
            return (
                ToolSpec(name="observe", description="Observe", input_schema={}),
                ToolSpec(name="assessment_prepare", description="Prepare", input_schema={}),
                ToolSpec(name="unrelated", description="Other", input_schema={}),
            )

        async def invoke(self, name, context, arguments):
            self.calls.append(name)
            if name == "observe":
                return ToolResult(
                    content="durable reviewed evidence",
                    metadata={"preferred_next_tool": "assessment_prepare"},
                )
            return ToolResult(content="prepared")

    class RefusingOnceProvider:
        def __init__(self):
            self.requests = []

        async def stream(self, request):
            self.requests.append(request)
            if len(self.requests) == 1:
                yield ToolCall(call_id="observe", name="observe", arguments={})
                yield ModelCompleted(finish_reason="tool_calls")
                return
            if len(self.requests) == 2:
                yield TextDelta(text="I cannot proceed because preparation looks unsafe.")
                yield ModelCompleted()
                return
            if len(self.requests) == 3:
                yield ToolCall(
                    call_id="prepare",
                    name="assessment_prepare",
                    arguments={},
                )
                yield ModelCompleted(finish_reason="tool_calls")
                return
            yield TextDelta(text="Prepared.")
            yield ModelCompleted()

    provider = RefusingOnceProvider()
    tools = FollowThroughTools()
    store = Store()
    kernel = Kernel(provider, store, tools, Settings())
    await kernel.start()
    await kernel.submit("prepare")
    await kernel.wait_for_turn()

    assert len(provider.requests) == 4
    assert all(
        [tool["function"]["name"] for tool in request.tools] == ["assessment_prepare"]
        for request in provider.requests[1:3]
    )
    assert "Durable tool evidence takes precedence" in provider.requests[2].messages[-1].content
    assert tools.calls == ["observe", "assessment_prepare"]
    uncertainties = [
        event for event in store.events if event.payload.get("code") == "evaluation_uncertain"
    ]
    assert len(uncertainties) == 1
    assert uncertainties[0].payload["nonblocking"] is True
    assert not any(
        message.role == "assistant" and "cannot proceed" in message.content
        for message in store.messages[kernel.session_id]
    )


@pytest.mark.asyncio
async def test_preferred_tool_cooperation_does_not_retry() -> None:
    class CooperativeTools(Tools):
        def __init__(self):
            self.calls = []

        def describe(self):
            return (
                ToolSpec(name="observe", description="Observe", input_schema={}),
                ToolSpec(name="assessment_prepare", description="Prepare", input_schema={}),
            )

        async def invoke(self, name, context, arguments):
            self.calls.append(name)
            if name == "observe":
                return ToolResult(
                    content="evidence",
                    metadata={"preferred_next_tool": "assessment_prepare"},
                )
            return ToolResult(content="prepared")

    class CooperativeProvider:
        def __init__(self):
            self.requests = []

        async def stream(self, request):
            self.requests.append(request)
            if len(self.requests) == 1:
                yield ToolCall(call_id="observe", name="observe", arguments={})
                yield ModelCompleted(finish_reason="tool_calls")
                return
            if len(self.requests) == 2:
                yield ToolCall(
                    call_id="prepare",
                    name="assessment_prepare",
                    arguments={},
                )
                yield ModelCompleted(finish_reason="tool_calls")
                return
            yield TextDelta(text="Prepared.")
            yield ModelCompleted()

    provider = CooperativeProvider()
    tools = CooperativeTools()
    store = Store()
    kernel = Kernel(provider, store, tools, Settings())
    await kernel.start()
    await kernel.submit("prepare")
    await kernel.wait_for_turn()

    assert len(provider.requests) == 3
    assert tools.calls == ["observe", "assessment_prepare"]
    assert not any(
        event.payload.get("code") in {"evaluation_uncertain", "preferred_tool_not_called"}
        for event in store.events
    )


@pytest.mark.asyncio
async def test_dynamic_preferred_tool_invocation_discharges_obligation() -> None:
    class DynamicTools(Tools):
        def describe(self):
            return (
                ToolSpec(name="observe", description="Observe", input_schema={}),
                ToolSpec(name="assessment_prepare", description="Prepare", input_schema={}),
            )

        async def invoke(self, name, context, arguments):
            if name == "observe":
                return ToolResult(
                    content="evidence",
                    metadata={"preferred_next_tool": "assessment_prepare"},
                )
            return ToolResult(content="prepared")

    class DynamicProvider:
        def __init__(self):
            self.requests = []

        async def stream(self, request):
            raise AssertionError("dynamic provider must use stream_with_tools")
            yield  # pragma: no cover

        async def stream_with_tools(self, request, handler):
            self.requests.append(request)
            if len(self.requests) == 1:
                result = await handler(ToolCall(call_id="observe", name="observe", arguments={}))
                assert result.success is True
                yield ModelCompleted(finish_reason="tool_calls")
                return
            result = await handler(
                ToolCall(
                    call_id="prepare",
                    name="assessment_prepare",
                    arguments={},
                )
            )
            assert result.success is True
            yield TextDelta(text="Prepared.")
            yield ModelCompleted()

    provider = DynamicProvider()
    store = Store()
    kernel = Kernel(provider, store, DynamicTools(), Settings())
    await kernel.start()
    await kernel.submit("prepare")
    await kernel.wait_for_turn()

    assert len(provider.requests) == 2
    assert [tool["function"]["name"] for tool in provider.requests[1].tools] == [
        "assessment_prepare"
    ]
    assert not any(
        event.payload.get("code") in {"evaluation_uncertain", "preferred_tool_not_called"}
        for event in store.events
    )


@pytest.mark.asyncio
async def test_repeated_preferred_tool_refusal_is_typed_retryable_failure() -> None:
    class PreferredTools(Tools):
        def describe(self):
            return (
                ToolSpec(name="observe", description="Observe", input_schema={}),
                ToolSpec(name="assessment_prepare", description="Prepare", input_schema={}),
            )

        async def invoke(self, name, context, arguments):
            if name == "observe":
                return ToolResult(
                    content="evidence",
                    metadata={"preferred_next_tool": "assessment_prepare"},
                )
            raise AssertionError("provider never called the preferred tool")

    class RefusingProvider:
        def __init__(self):
            self.requests = []

        async def stream(self, request):
            self.requests.append(request)
            if len(self.requests) == 1:
                yield ToolCall(call_id="observe", name="observe", arguments={})
                yield ModelCompleted(finish_reason="tool_calls")
                return
            yield TextDelta(text="I refuse to call that tool.")
            yield ModelCompleted()

    provider = RefusingProvider()
    store = Store()
    kernel = Kernel(
        provider,
        store,
        PreferredTools(),
        Settings(),
        max_provider_retries=2,
    )
    await kernel.start()
    await kernel.submit("prepare")
    await kernel.wait_for_turn()

    assert len(provider.requests) == 4
    assert all(
        [tool["function"]["name"] for tool in request.tools] == ["assessment_prepare"]
        for request in provider.requests[1:]
    )
    errors = [event for event in store.events if event.type == "error"]
    assert errors[-1].payload["code"] == "preferred_tool_not_called"
    assert errors[-1].payload["retryable"] is True
    assert errors[-1].payload["failure_class"] == "infrastructure"
    assert (
        len(
            [event for event in store.events if event.payload.get("code") == "evaluation_uncertain"]
        )
        == 3
    )
    assert not [
        message
        for message in store.messages[kernel.session_id]
        if message.role == "assistant" and message.content
    ]


@pytest.mark.asyncio
async def test_wrong_tool_cannot_evade_preferred_tool_retry_budget() -> None:
    class PreferredTools(Tools):
        def __init__(self):
            self.calls = []

        def describe(self):
            return (
                ToolSpec(name="observe", description="Observe", input_schema={}),
                ToolSpec(name="assessment_prepare", description="Prepare", input_schema={}),
                ToolSpec(name="unrelated", description="Other", input_schema={}),
            )

        async def invoke(self, name, context, arguments):
            self.calls.append(name)
            if name == "observe":
                return ToolResult(
                    content="evidence",
                    metadata={"preferred_next_tool": "assessment_prepare"},
                )
            return ToolResult(content="alternate tool completed")

    class WrongToolProvider:
        def __init__(self):
            self.requests = []

        async def stream(self, request):
            self.requests.append(request)
            if len(self.requests) == 1:
                yield ToolCall(call_id="observe", name="observe", arguments={})
                yield ModelCompleted(finish_reason="tool_calls")
                return
            yield ToolCall(
                call_id=f"wrong-{len(self.requests)}",
                name="unrelated",
                arguments={},
            )
            yield ModelCompleted(finish_reason="tool_calls")

    provider = WrongToolProvider()
    tools = PreferredTools()
    store = Store()
    kernel = Kernel(
        provider,
        store,
        tools,
        Settings(),
        max_provider_retries=2,
    )
    await kernel.start()
    await kernel.submit("prepare")
    await kernel.wait_for_turn()

    assert len(provider.requests) == 4
    assert all(
        [tool["function"]["name"] for tool in request.tools] == ["assessment_prepare"]
        for request in provider.requests[1:]
    )
    assert tools.calls == ["observe"]
    assert (
        len(
            [event for event in store.events if event.payload.get("code") == "evaluation_uncertain"]
        )
        == 3
    )
    errors = [event for event in store.events if event.type == "error"]
    assert errors[-1].payload["code"] == "preferred_tool_not_called"
    assert errors[-1].payload["retryable"] is True


@pytest.mark.asyncio
async def test_preferred_tool_integrity_rejection_remains_real_tool_error() -> None:
    class RejectingTools(Tools):
        def __init__(self):
            self.calls = []

        def describe(self):
            return (
                ToolSpec(name="observe", description="Observe", input_schema={}),
                ToolSpec(name="assessment_prepare", description="Prepare", input_schema={}),
            )

        async def invoke(self, name, context, arguments):
            self.calls.append(name)
            if name == "observe":
                return ToolResult(
                    content="evidence",
                    metadata={"preferred_next_tool": "assessment_prepare"},
                )
            return ToolResult(content="manifest digest mismatch", is_error=True)

    class IntegrityProvider:
        def __init__(self):
            self.requests = []

        async def stream(self, request):
            self.requests.append(request)
            if len(self.requests) == 1:
                yield ToolCall(call_id="observe", name="observe", arguments={})
                yield ModelCompleted(finish_reason="tool_calls")
                return
            if len(self.requests) == 2:
                yield ToolCall(
                    call_id="prepare",
                    name="assessment_prepare",
                    arguments={},
                )
                yield ModelCompleted(finish_reason="tool_calls")
                return
            yield TextDelta(text="Preparation was rejected by the tool.")
            yield ModelCompleted()

    provider = IntegrityProvider()
    tools = RejectingTools()
    store = Store()
    kernel = Kernel(provider, store, tools, Settings())
    await kernel.start()
    await kernel.submit("prepare")
    await kernel.wait_for_turn()

    assert len(provider.requests) == 3
    assert tools.calls == ["observe", "assessment_prepare"]
    assert any(
        event.type == "tool"
        and event.payload.get("name") == "assessment_prepare"
        and event.payload.get("state") == "failed"
        and event.payload.get("text") == "manifest digest mismatch"
        for event in store.events
    )
    assert not any(
        event.payload.get("code") == "preferred_tool_not_called" for event in store.events
    )


@pytest.mark.asyncio
async def test_cleanup_discovery_continues_recon_without_operator_prompt() -> None:
    class ReconTools(Tools):
        def __init__(self):
            self.prepare_calls = 0
            self.calls = []

        def describe(self):
            return (
                ToolSpec(name="assessment_prepare", description="Prepare", input_schema={}),
                ToolSpec(name="browser_snapshot", description="Inspect", input_schema={}),
                ToolSpec(
                    name="browser_end_owned_session",
                    description="End owned session",
                    input_schema={},
                ),
                ToolSpec(name="browser_network_events", description="Network", input_schema={}),
                ToolSpec(
                    name="browser_compile_api_handoff", description="Compile", input_schema={}
                ),
                ToolSpec(name="unrelated", description="Unrelated", input_schema={}),
            )

        async def invoke(self, name, context, arguments):
            self.calls.append(name)
            if name == "assessment_prepare":
                self.prepare_calls += 1
                if self.prepare_calls == 1:
                    return ToolResult(
                        content="cleanup discovery required",
                        is_error=True,
                        metadata={
                            "automatic_recon_continuation": _cleanup_continuation(
                                "handoff:one",
                                ownership="owned",
                                ownership_evidence_refs=[
                                    "creation:before",
                                    "creation:after",
                                ],
                            )
                        },
                    )
                return ToolResult(content="campaign prepared")
            if name == "browser_end_owned_session":
                return ToolResult(
                    content="owned session-ending control clicked",
                    metadata={
                        "evidence_refs": ["cleanup:action", "cleanup:response"],
                        "preferred_next_tool": "browser_compile_api_handoff",
                    },
                )
            if name == "browser_compile_api_handoff":
                return ToolResult(
                    content=json.dumps({"id": "handoff:superseding"}),
                    metadata={
                        "compiled_adapter": True,
                        "evidence_refs": ["cleanup:action", "cleanup:response"],
                        "preferred_next_tool": "assessment_prepare",
                    },
                )
            return ToolResult(content="fresh owned lifecycle observed")

    class ReconProvider:
        def __init__(self):
            self.requests = []

        async def stream(self, request):
            self.requests.append(request)
            index = len(self.requests)
            if index == 1:
                yield ToolCall(call_id="prepare-1", name="assessment_prepare", arguments={})
                yield ModelCompleted(finish_reason="tool_calls")
            elif index == 2:
                yield ToolCall(
                    call_id="cleanup-1",
                    name="browser_end_owned_session",
                    arguments={},
                )
                yield ModelCompleted(finish_reason="tool_calls")
            elif index == 3:
                yield ToolCall(
                    call_id="compile-1", name="browser_compile_api_handoff", arguments={}
                )
                yield ModelCompleted(finish_reason="tool_calls")
            elif index == 4:
                yield ToolCall(call_id="prepare-2", name="assessment_prepare", arguments={})
                yield ModelCompleted(finish_reason="tool_calls")
            else:
                yield TextDelta(text="prepared")
                yield ModelCompleted()

    provider = ReconProvider()
    tools = ReconTools()
    kernel = Kernel(provider, Store(), tools, Settings())
    await kernel.start()
    await kernel.submit("run autonomous assessment")
    await kernel.wait_for_turn()

    assert tools.prepare_calls == 2
    assert all(
        tool["function"]["name"] != "browser_end_owned_session"
        for tool in provider.requests[0].tools
    )
    assert [tool["function"]["name"] for tool in provider.requests[1].tools] == [
        "browser_end_owned_session"
    ]
    assert any(
        "exact owned reconnaissance session" in message.content
        for message in provider.requests[1].messages
        if message.role == "system"
    )
    assert [tool["function"]["name"] for tool in provider.requests[3].tools] == [
        "assessment_prepare"
    ]
    assert "browser_network_events" not in tools.calls
    assert all(
        "operator for permission" not in message.content.lower()
        for request in provider.requests
        for message in request.messages
        if message.role == "assistant"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("alternate", ["browser_network_events", "browser_compile_api_handoff"])
async def test_owned_cleanup_rejects_alternate_lifecycle_tool_before_cleanup(
    alternate: str,
) -> None:
    class ReconTools(Tools):
        def __init__(self):
            self.calls = []
            self.prepare_calls = 0

        def describe(self):
            return tuple(
                ToolSpec(name=name, description=name, input_schema={})
                for name in (
                    "assessment_prepare",
                    "browser_end_owned_session",
                    "browser_network_events",
                    "browser_compile_api_handoff",
                )
            )

        async def invoke(self, name, context, arguments):
            self.calls.append(name)
            if name == "assessment_prepare":
                self.prepare_calls += 1
                if self.prepare_calls > 1:
                    return ToolResult(content="prepared")
                return ToolResult(
                    content="held",
                    is_error=True,
                    metadata={
                        "automatic_recon_continuation": _cleanup_continuation(
                            "handoff:alternate",
                            ownership="owned",
                            ownership_evidence_refs=["creation:before", "creation:after"],
                        )
                    },
                )
            if name == "browser_end_owned_session":
                return ToolResult(
                    content="cleaned",
                    metadata={"preferred_next_tool": "browser_compile_api_handoff"},
                )
            if name == "browser_compile_api_handoff":
                return ToolResult(
                    content="compiled",
                    metadata={"preferred_next_tool": "assessment_prepare"},
                )
            return ToolResult(content="optional lifecycle observation")

    class AlternateFirstProvider:
        def __init__(self):
            self.requests = []

        async def stream(self, request):
            self.requests.append(request)
            names = (
                "assessment_prepare",
                alternate,
                "browser_end_owned_session",
                "browser_compile_api_handoff",
                "assessment_prepare",
            )
            name = names[len(self.requests) - 1]
            yield ToolCall(call_id=f"call-{len(self.requests)}", name=name, arguments={})
            yield ModelCompleted(finish_reason="tool_calls")

    store = Store()
    provider = AlternateFirstProvider()
    tools = ReconTools()
    kernel = Kernel(provider, store, tools, Settings())
    await kernel.start()
    await kernel.submit("run")
    await kernel.wait_for_turn()

    assert tools.calls == [
        "assessment_prepare",
        "browser_end_owned_session",
        "browser_compile_api_handoff",
        "assessment_prepare",
    ]
    assert [tool["function"]["name"] for tool in provider.requests[1].tools] == [
        "browser_end_owned_session"
    ]
    assert any(event.payload.get("code") == "evaluation_uncertain" for event in store.events)


@pytest.mark.asyncio
async def test_owned_cleanup_failure_stops_retryably_without_compile() -> None:
    class FailingCleanupTools(Tools):
        def __init__(self):
            self.calls = []

        def describe(self):
            return tuple(
                ToolSpec(name=name, description=name, input_schema={})
                for name in (
                    "assessment_prepare",
                    "browser_end_owned_session",
                    "browser_compile_api_handoff",
                )
            )

        async def invoke(self, name, context, arguments):
            self.calls.append(name)
            if name == "assessment_prepare":
                return ToolResult(
                    content="held",
                    is_error=True,
                    metadata={
                        "automatic_recon_continuation": _cleanup_continuation(
                            "handoff:cleanup-failure",
                            ownership="owned",
                            ownership_evidence_refs=["creation:before", "creation:after"],
                        )
                    },
                )
            return ToolResult(
                content="cleanup transport failed",
                is_error=True,
                metadata={
                    "infrastructure_failure_category": (
                        InfrastructureFailureCategory.DELIVERY_FAILURE.value
                    )
                },
            )

    class CleanupProvider:
        def __init__(self):
            self.requests = []

        async def stream(self, request):
            self.requests.append(request)
            name = "assessment_prepare" if len(self.requests) == 1 else "browser_end_owned_session"
            yield ToolCall(call_id=f"call-{len(self.requests)}", name=name, arguments={})
            yield ModelCompleted(finish_reason="tool_calls")

    store = Store()
    tools = FailingCleanupTools()
    kernel = Kernel(CleanupProvider(), store, tools, Settings())
    await kernel.start()
    await kernel.submit("run")
    await kernel.wait_for_turn()

    assert tools.calls == ["assessment_prepare", "browser_end_owned_session"]
    errors = [event for event in store.events if event.type == "error"]
    assert errors[-1].payload["code"] == "owned_session_cleanup_failed"
    assert errors[-1].payload["retryable"] is True
    assert errors[-1].payload["failure_class"] == "infrastructure"
    assert (
        errors[-1].payload["infrastructure_failure_category"]
        == InfrastructureFailureCategory.DELIVERY_FAILURE.value
    )


@pytest.mark.asyncio
async def test_owned_cleanup_runs_at_most_once_in_static_tool_batch() -> None:
    class DuplicateCleanupTools(Tools):
        def __init__(self):
            self.calls = []
            self.prepare_calls = 0

        def describe(self):
            return tuple(
                ToolSpec(name=name, description=name, input_schema={})
                for name in (
                    "assessment_prepare",
                    "browser_end_owned_session",
                    "browser_compile_api_handoff",
                )
            )

        async def invoke(self, name, context, arguments):
            self.calls.append(name)
            if name == "assessment_prepare":
                self.prepare_calls += 1
                if self.prepare_calls > 1:
                    return ToolResult(content="prepared")
                return ToolResult(
                    content="held",
                    is_error=True,
                    metadata={
                        "automatic_recon_continuation": _cleanup_continuation(
                            "handoff:duplicate-static",
                            ownership="owned",
                            ownership_evidence_refs=["creation:before", "creation:after"],
                        )
                    },
                )
            if name == "browser_end_owned_session":
                return ToolResult(
                    content="cleaned",
                    metadata={"preferred_next_tool": "browser_compile_api_handoff"},
                )
            return ToolResult(
                content="compiled",
                metadata={"preferred_next_tool": "assessment_prepare"},
            )

    class DuplicateCleanupProvider:
        def __init__(self):
            self.requests = []

        async def stream(self, request):
            self.requests.append(request)
            if len(self.requests) == 1:
                yield ToolCall(call_id="prepare-1", name="assessment_prepare", arguments={})
            elif len(self.requests) == 2:
                yield ToolCall(call_id="cleanup-1", name="browser_end_owned_session", arguments={})
                yield ToolCall(call_id="cleanup-2", name="browser_end_owned_session", arguments={})
            elif len(self.requests) == 3:
                yield ToolCall(call_id="compile", name="browser_compile_api_handoff", arguments={})
            elif len(self.requests) == 4:
                yield ToolCall(call_id="prepare-2", name="assessment_prepare", arguments={})
            else:
                yield TextDelta(text="prepared")
            yield ModelCompleted(finish_reason="tool_calls")

    store = Store()
    tools = DuplicateCleanupTools()
    kernel = Kernel(DuplicateCleanupProvider(), store, tools, Settings())
    session_id = await kernel.start()
    await kernel.submit("run")
    await kernel.wait_for_turn()

    assert tools.calls.count("browser_end_owned_session") == 1
    duplicate_results = [
        message
        for message in store.messages[session_id]
        if message.role == "tool" and message.tool_call_id == "cleanup-2"
    ]
    assert len(duplicate_results) == 1
    assert "not admitted" in duplicate_results[0].content


@pytest.mark.asyncio
async def test_owned_cleanup_runs_at_most_once_for_dynamic_provider() -> None:
    class DynamicCleanupTools(Tools):
        def __init__(self):
            self.calls = []
            self.prepare_calls = 0

        def describe(self):
            return tuple(
                ToolSpec(name=name, description=name, input_schema={})
                for name in (
                    "assessment_prepare",
                    "browser_end_owned_session",
                    "browser_compile_api_handoff",
                )
            )

        async def invoke(self, name, context, arguments):
            self.calls.append(name)
            if name == "assessment_prepare":
                self.prepare_calls += 1
                if self.prepare_calls > 1:
                    return ToolResult(content="prepared")
                return ToolResult(
                    content="held",
                    is_error=True,
                    metadata={
                        "automatic_recon_continuation": _cleanup_continuation(
                            "handoff:duplicate-dynamic",
                            ownership="owned",
                            ownership_evidence_refs=["creation:before", "creation:after"],
                        )
                    },
                )
            if name == "browser_end_owned_session":
                return ToolResult(
                    content="cleaned",
                    metadata={"preferred_next_tool": "browser_compile_api_handoff"},
                )
            return ToolResult(
                content="compiled",
                metadata={"preferred_next_tool": "assessment_prepare"},
            )

    class DynamicCleanupProvider:
        def __init__(self):
            self.requests = []
            self.duplicate_result = None

        async def stream(self, request):
            raise AssertionError("dynamic provider must use stream_with_tools")
            yield

        async def stream_with_tools(self, request, handler):
            self.requests.append(request)
            if len(self.requests) == 1:
                await handler(
                    ToolCall(call_id="prepare-1", name="assessment_prepare", arguments={})
                )
            elif len(self.requests) == 2:
                await handler(
                    ToolCall(call_id="cleanup-1", name="browser_end_owned_session", arguments={})
                )
                self.duplicate_result = await handler(
                    ToolCall(call_id="cleanup-2", name="browser_end_owned_session", arguments={})
                )
            elif len(self.requests) == 3:
                await handler(
                    ToolCall(call_id="compile", name="browser_compile_api_handoff", arguments={})
                )
            else:
                await handler(
                    ToolCall(call_id="prepare-2", name="assessment_prepare", arguments={})
                )
                yield TextDelta(text="prepared")
            yield ModelCompleted()

    tools = DynamicCleanupTools()
    provider = DynamicCleanupProvider()
    kernel = Kernel(provider, Store(), tools, Settings())
    await kernel.start()
    await kernel.submit("run")
    await kernel.wait_for_turn()

    assert tools.calls.count("browser_end_owned_session") == 1
    assert provider.duplicate_result == DynamicToolOutput(
        content="browser_end_owned_session failed: tool is not admitted for this provider request",
        success=False,
    )
    assert tools.calls[-2:] == ["browser_compile_api_handoff", "assessment_prepare"]


@pytest.mark.asyncio
@pytest.mark.parametrize("second_handoff", ["handoff:repeated-hold", "handoff:superseding"])
async def test_repeated_owned_cleanup_hold_fails_evidence_without_repeating_cleanup(
    second_handoff: str,
) -> None:
    class RepeatedHoldTools(Tools):
        def __init__(self):
            self.calls = []
            self.prepare_calls = 0

        def describe(self):
            return tuple(
                ToolSpec(name=name, description=name, input_schema={})
                for name in (
                    "assessment_prepare",
                    "browser_end_owned_session",
                    "browser_compile_api_handoff",
                )
            )

        async def invoke(self, name, context, arguments):
            self.calls.append(name)
            if name == "assessment_prepare":
                self.prepare_calls += 1
                return ToolResult(
                    content="cleanup evidence still missing",
                    is_error=True,
                    metadata={
                        "automatic_recon_continuation": _cleanup_continuation(
                            (
                                "handoff:repeated-hold"
                                if self.prepare_calls == 1
                                else second_handoff
                            ),
                            ownership="owned",
                            ownership_evidence_refs=["creation:before", "creation:after"],
                        )
                    },
                )
            if name == "browser_end_owned_session":
                return ToolResult(
                    content="cleaned",
                    metadata={"preferred_next_tool": "browser_compile_api_handoff"},
                )
            return ToolResult(
                content="compiled",
                metadata={"preferred_next_tool": "assessment_prepare"},
            )

    class RepeatedHoldProvider:
        def __init__(self):
            self.calls = 0

        async def stream(self, request):
            self.calls += 1
            name = (
                "assessment_prepare",
                "browser_end_owned_session",
                "browser_compile_api_handoff",
                "assessment_prepare",
            )[self.calls - 1]
            yield ToolCall(call_id=f"call-{self.calls}", name=name, arguments={})
            yield ModelCompleted(finish_reason="tool_calls")

    store = Store()
    tools = RepeatedHoldTools()
    kernel = Kernel(RepeatedHoldProvider(), store, tools, Settings())
    await kernel.start()
    await kernel.submit("run")
    await kernel.wait_for_turn()

    assert tools.calls.count("browser_end_owned_session") == 1
    assert tools.calls == [
        "assessment_prepare",
        "browser_end_owned_session",
        "browser_compile_api_handoff",
        "assessment_prepare",
    ]
    errors = [event for event in store.events if event.type == "error"]
    assert errors[-1].payload["code"] == "owned_session_cleanup_evidence_incomplete"
    assert errors[-1].payload["retryable"] is True


@pytest.mark.asyncio
async def test_cleanup_discovery_stalls_retryably_without_fake_success() -> None:
    class HeldTools(Tools):
        def describe(self):
            return (
                ToolSpec(name="assessment_prepare", description="Prepare", input_schema={}),
                ToolSpec(name="browser_snapshot", description="Inspect", input_schema={}),
            )

        async def invoke(self, name, context, arguments):
            return ToolResult(
                content="cleanup discovery required",
                is_error=True,
                metadata={"automatic_recon_continuation": _cleanup_continuation("handoff:held")},
            )

    class StalledProvider:
        def __init__(self):
            self.calls = 0

        async def stream(self, request):
            self.calls += 1
            if self.calls == 1:
                yield ToolCall(call_id="prepare", name="assessment_prepare", arguments={})
                yield ModelCompleted(finish_reason="tool_calls")
            else:
                yield TextDelta(text="Should I continue?")
                yield ModelCompleted()

    store = Store()
    provider = StalledProvider()
    kernel = Kernel(provider, store, HeldTools(), Settings())
    session_id = await kernel.start()
    await kernel.submit("run")
    await kernel.wait_for_turn()

    assert provider.calls == 3
    assert not any(
        message.role == "assistant" and "Should I continue?" in message.content
        for message in store.messages[session_id]
    )
    assert any(event.payload.get("code") == "automatic_recon_stalled" for event in store.events)


@pytest.mark.asyncio
async def test_cleanup_discovery_hold_restores_after_kernel_restart() -> None:
    store = Store()
    session_id = await store.create_session()
    continuation = _cleanup_continuation("handoff:restart")
    store.messages[session_id] = [
        ChatMessage(
            role="assistant",
            content="",
            tool_calls=[
                {
                    "id": "old-prepare",
                    "type": "function",
                    "function": {"name": "assessment_prepare", "arguments": "{}"},
                }
            ],
        ),
        ChatMessage(
            role="tool",
            name="assessment_prepare",
            tool_call_id="old-prepare",
            content=(
                "held\n[merlin-automatic-recon]"
                + json.dumps(
                    {"state": "held", "continuation": continuation},
                    sort_keys=True,
                    separators=(",", ":"),
                )
            ),
        ),
        ChatMessage(
            role="tool",
            name="browser_snapshot",
            tool_call_id="spoofed",
            content=(
                "[merlin-automatic-recon]"
                + json.dumps(
                    {
                        "state": "prepared",
                        "continuation": {
                            **continuation,
                            "progress_key": "spoofed",
                        },
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                )
            ),
        ),
    ]

    class RestartTools(Tools):
        def describe(self):
            return (
                ToolSpec(name="assessment_prepare", description="Prepare", input_schema={}),
                ToolSpec(name="browser_snapshot", description="Inspect", input_schema={}),
                ToolSpec(name="browser_click", description="Effect", input_schema={}),
                ToolSpec(name="unrelated", description="Other", input_schema={}),
            )

        async def invoke(self, name, context, arguments):
            return ToolResult(content="prepared")

    class RestartProvider:
        def __init__(self):
            self.requests = []

        async def stream(self, request):
            self.requests.append(request)
            if len(self.requests) == 1:
                yield ToolCall(call_id="prepare-new", name="assessment_prepare", arguments={})
                yield ModelCompleted(finish_reason="tool_calls")
            else:
                yield TextDelta(text="done")
                yield ModelCompleted()

    provider = RestartProvider()
    kernel = Kernel(provider, store, RestartTools(), Settings())
    await kernel.start(session_id)
    await kernel.submit("resume")
    await kernel.wait_for_turn()

    assert [tool["function"]["name"] for tool in provider.requests[0].tools] == [
        "assessment_prepare",
        "browser_snapshot",
    ]


@pytest.mark.asyncio
async def test_cleanup_progress_plateau_persists_across_restart() -> None:
    store = Store()
    session_id = await store.create_session()
    continuation = _cleanup_continuation("handoff:persisted")
    progress = json.dumps(
        {
            "progress_key": "handoff:persisted:session_not_owned",
            "stage": "browser_snapshot",
            "evidence_refs": ["snapshot:evidence"],
            "superseding_handoff": None,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    store.messages[session_id] = [
        ChatMessage(
            role="assistant",
            content="",
            tool_calls=[
                {
                    "id": "prepare-old",
                    "function": {"name": "assessment_prepare", "arguments": "{}"},
                },
                {
                    "id": "snapshot-old",
                    "function": {"name": "browser_snapshot", "arguments": "{}"},
                },
            ],
        ),
        ChatMessage(
            role="tool",
            name="assessment_prepare",
            tool_call_id="prepare-old",
            content=(
                "held\n[merlin-automatic-recon]"
                + json.dumps(
                    {"state": "held", "continuation": continuation},
                    sort_keys=True,
                    separators=(",", ":"),
                )
            ),
        ),
        ChatMessage(
            role="tool",
            name="browser_snapshot",
            tool_call_id="snapshot-old",
            content=(
                "snapshot\n[merlin-automatic-recon]"
                + json.dumps(
                    {"state": "progress", "progress": progress},
                    sort_keys=True,
                    separators=(",", ":"),
                )
            ),
        ),
    ]

    class RestartTools(Tools):
        def describe(self):
            return (ToolSpec(name="browser_snapshot", description="Inspect", input_schema={}),)

        async def invoke(self, name, context, arguments):
            return ToolResult(
                content="different prose",
                metadata={"evidence_refs": ["snapshot:evidence"]},
            )

    class RestartProvider:
        async def stream(self, request):
            yield ToolCall(call_id="snapshot-new", name="browser_snapshot", arguments={})
            yield ModelCompleted(finish_reason="tool_calls")

    kernel = Kernel(RestartProvider(), store, RestartTools(), Settings())
    await kernel.start(session_id)
    await kernel.submit("resume")
    await kernel.wait_for_turn()

    assert any(event.payload.get("code") == "automatic_recon_stalled" for event in store.events)


@pytest.mark.asyncio
async def test_cleanup_discovery_batch_later_prepare_success_clears_hold() -> None:
    class BatchTools(Tools):
        async def invoke(self, name, context, arguments):
            if arguments.get("held") is True:
                return ToolResult(
                    content="held",
                    is_error=True,
                    metadata={
                        "automatic_recon_continuation": _cleanup_continuation("handoff:batch")
                    },
                )
            return ToolResult(content="prepared")

    kernel = Kernel(Provider(), Store(), BatchTools(), Settings())
    session_id = await kernel.start()
    _, continuation, _, prepare_succeeded = await kernel._invoke_tools(  # noqa: SLF001
        session_id,
        [
            ToolCall(call_id="held", name="assessment_prepare", arguments={"held": True}),
            ToolCall(call_id="success", name="assessment_prepare", arguments={}),
        ],
    )

    assert continuation is None
    assert prepare_succeeded is True


@pytest.mark.asyncio
async def test_cleanup_discovery_detects_non_consecutive_semantic_plateau() -> None:
    class CycleTools(Tools):
        def __init__(self):
            self.noise = 0

        def describe(self):
            return (
                ToolSpec(name="assessment_prepare", description="Prepare", input_schema={}),
                ToolSpec(name="browser_snapshot", description="Inspect", input_schema={}),
                ToolSpec(name="browser_network_events", description="Network", input_schema={}),
            )

        async def invoke(self, name, context, arguments):
            self.noise += 1
            if name == "assessment_prepare":
                return ToolResult(
                    content="held",
                    is_error=True,
                    metadata={
                        "automatic_recon_continuation": _cleanup_continuation("handoff:cycle")
                    },
                )
            return ToolResult(content=f"changing presentation noise {self.noise}")

    class CycleProvider:
        def __init__(self):
            self.calls = 0

        async def stream(self, request):
            self.calls += 1
            names = (
                "assessment_prepare",
                "browser_snapshot",
                "browser_network_events",
                "browser_snapshot",
            )
            name = names[min(self.calls - 1, len(names) - 1)]
            yield ToolCall(call_id=f"call-{self.calls}", name=name, arguments={})
            yield ModelCompleted(finish_reason="tool_calls")

    store = Store()
    provider = CycleProvider()
    kernel = Kernel(provider, store, CycleTools(), Settings())
    await kernel.start()
    await kernel.submit("run")
    await kernel.wait_for_turn()

    assert provider.calls == 4
    assert any(event.payload.get("code") == "automatic_recon_stalled" for event in store.events)


@pytest.mark.asyncio
async def test_repeated_cleanup_hold_with_same_key_retains_progress_plateau() -> None:
    class ResetTools(Tools):
        def __init__(self):
            self.prepare = 0

        def describe(self):
            return (
                ToolSpec(name="assessment_prepare", description="Prepare", input_schema={}),
                ToolSpec(name="browser_snapshot", description="Inspect", input_schema={}),
            )

        async def invoke(self, name, context, arguments):
            if name == "assessment_prepare":
                self.prepare += 1
                if self.prepare < 3:
                    return ToolResult(
                        content="held",
                        is_error=True,
                        metadata={
                            "automatic_recon_continuation": _cleanup_continuation(
                                "handoff:same-key-new-generation"
                            )
                        },
                    )
                return ToolResult(content="prepared")
            return ToolResult(
                content="snapshot presentation changed",
                metadata={"evidence_refs": ["snapshot:evidence"]},
            )

    class ResetProvider:
        def __init__(self):
            self.calls = 0

        async def stream(self, request):
            self.calls += 1
            names = (
                "assessment_prepare",
                "browser_snapshot",
                "assessment_prepare",
                "browser_snapshot",
                "assessment_prepare",
            )
            if self.calls <= len(names):
                name = names[self.calls - 1]
                yield ToolCall(call_id=f"reset-{self.calls}", name=name, arguments={})
                yield ModelCompleted(finish_reason="tool_calls")
            else:
                yield TextDelta(text="done")
                yield ModelCompleted()

    store = Store()
    provider = ResetProvider()
    kernel = Kernel(provider, store, ResetTools(), Settings())
    await kernel.start()
    await kernel.submit("run")
    await kernel.wait_for_turn()

    assert provider.calls == 4
    assert any(event.payload.get("code") == "automatic_recon_stalled" for event in store.events)


@pytest.mark.asyncio
async def test_malformed_later_tool_call_retries_without_executing_abandoned_batch() -> None:
    class RecordingTools(Tools):
        def __init__(self):
            self.calls = []

        def describe(self):
            return (
                ToolSpec(name="browser_fill", description="Fill", input_schema={}),
                ToolSpec(name="browser_wait_for_text", description="Wait", input_schema={}),
            )

        async def invoke(self, name, context, arguments):
            self.calls.append((name, arguments))
            return ToolResult(content="ok")

    class RepairingProvider:
        def __init__(self):
            self.requests = []

        async def stream(self, request):
            self.requests.append(request)
            if len(self.requests) == 1:
                yield TextDelta(text="Now submit the probe and wait for the response.")
                yield ToolCall(
                    call_id="abandoned-fill",
                    name="browser_fill",
                    arguments={"ref": "chat", "text": "harmless probe"},
                )
                yield ModelError(
                    message=(
                        "provider returned malformed tool arguments "
                        "(assembly=fragments, chunks=61, bytes=108, digest=fixture)"
                    ),
                    code="malformed_tool_call",
                    retryable=True,
                    partial=True,
                    tool_name="browser_wait_for_text",
                )
                return
            if len(self.requests) == 2:
                yield ToolCall(
                    call_id="recovered-wait",
                    name="browser_wait_for_text",
                    arguments={"text": "FareCare"},
                )
                yield ModelCompleted(finish_reason="tool_calls")
                return
            yield TextDelta(text="Reconnaissance continued.")
            yield ModelCompleted()

    provider = RepairingProvider()
    tools = RecordingTools()
    store = Store()
    kernel = Kernel(provider, store, tools, Settings(), max_provider_retries=2)
    await kernel.start()
    await kernel.submit("recon")
    await kernel.wait_for_turn()

    assert len(provider.requests) == 3
    assert tools.calls == [("browser_wait_for_text", {"text": "FareCare"})]
    assert [event.payload.get("code") for event in store.events].count("provider_retry") == 1
    assert not any(event.type == "error" for event in store.events)
    assert store.messages[kernel.session_id][-1].content == "Reconnaissance continued."


@pytest.mark.asyncio
async def test_readiness_commands_and_resume_project_sanitized_durable_state():
    class Readiness:
        async def doctor(self):
            return {"healthy": True, "checks": [{"name": "postgres", "status": "ok"}]}

        async def logs(self, *, limit=100):
            return [{"level": "info", "message": "Bearer [redacted]", "limit": limit}]

        async def history(self, session_id, *, limit=100):
            return [{"kind": "session", "session_id": session_id, "limit": limit}]

        async def resume(self, session_id):
            return {
                "session_id": session_id,
                "recovered": 1,
                "messages": [{"role": "user", "content": "existing message"}],
                "schedules": [{"id": "schedule-1", "status": "running"}],
            }

        async def export_config(self):
            return '{"api_key":"[redacted]","model":"safe"}\n'

        async def support_bundle(self, output, session_id=None):
            return f"{output}:{session_id}"

    store = Store()
    existing = await store.create_session()
    kernel = Kernel(Provider(), store, Tools(), Settings(), readiness=Readiness())
    await kernel.start(existing)
    for command in (
        "/doctor",
        "/doctor config",
        "/logs 5",
        "/history 7",
        "/support diagnostics.zip",
        f"/resume {existing}",
    ):
        await kernel.submit(command)

    payloads = [event.payload for event in store.events]
    rendered = "\n".join(str(payload) for payload in payloads)
    assert "existing message" in rendered
    assert "schedule-1" in rendered
    assert "Bearer [redacted]" in rendered
    assert "session resumed" in rendered
    assert "diagnostics.zip" in rendered
    assert "hunter2" not in rendered


class Packs:
    def __init__(self):
        self.calls = []

    async def list_packs(self):
        self.calls.append(("list",))
        return [{"id": "starter"}]

    async def inspect_pack(self, pack_id):
        self.calls.append(("inspect", pack_id))
        return {"id": pack_id, "title": "Starter"}

    async def enable_pack(self, pack_id):
        self.calls.append(("enable", pack_id))
        return {"id": pack_id, "enabled": True}

    async def disable_pack(self, pack_id):
        self.calls.append(("disable", pack_id))
        return {"id": pack_id, "enabled": False}

    async def run_pack(self, pack_id=None, session_id=None):
        self.calls.append(("run", pack_id))
        yield {"state": "turn", "role": "attacker", "text": "hello"}


class Targets:
    def __init__(self):
        self.calls = []

    async def list_targets(self):
        self.calls.append(("list",))
        return [{"id": "local"}]

    async def add_target(self, arguments):
        self.calls.append(("add", *arguments))
        return {"id": arguments[0]}

    async def select_target(self, target_id):
        self.calls.append(("select", target_id))
        return {"id": target_id, "selected": True}

    async def remove_target(self, target_id):
        self.calls.append(("remove", target_id))
        return {"id": target_id, "removed": True}


def test_routes_target_service_without_exposing_pack_execution():
    asyncio.run(_routes_target_service_without_exposing_pack_execution())


async def _routes_target_service_without_exposing_pack_execution():
    store, packs, targets = Store(), Packs(), Targets()
    kernel = Kernel(Provider(), store, Tools(), Settings(), packs=packs, targets=targets)
    await kernel.start()
    await kernel.submit('/targets add local "http://localhost:1234/v1"')
    await kernel.submit("/targets select local")

    assert packs.calls == []
    assert targets.calls == [
        ("add", "local", "http://localhost:1234/v1"),
        ("select", "local"),
    ]


class Skills:
    def __init__(self):
        self.calls = []

    async def list_skills(self, session_id):
        self.calls.append(("list",))
        return [{"name": "reviewer"}]

    async def inspect_skill(self, name):
        self.calls.append(("inspect", name))
        return {"name": name, "instructions": "Review carefully."}

    async def enable(self, session_id, name):
        self.calls.append(("enable", session_id, name))
        return {"name": name, "enabled": True}

    async def disable(self, session_id, name):
        self.calls.append(("disable", session_id, name))
        return {"name": name, "enabled": False}

    async def instructions_for(self, session_id, user_text):
        self.calls.append(("instructions", session_id, user_text))
        return ["Complete reviewer instructions."]


def test_routes_skills_with_session_scope():
    asyncio.run(_routes_skills_with_session_scope())


async def _routes_skills_with_session_scope():
    store, skills = Store(), Skills()
    kernel = Kernel(Provider(), store, Tools(), Settings(), skills=skills)
    session = await kernel.start()
    await kernel.submit("/skills list")
    await kernel.submit("/skills inspect reviewer")
    await kernel.submit("/skills enable reviewer")
    await kernel.submit("/skills disable reviewer")

    assert skills.calls == [
        ("list",),
        ("inspect", "reviewer"),
        ("enable", session, "reviewer"),
        ("disable", session, "reviewer"),
    ]


def test_injects_skill_instructions_without_persisting_them():
    asyncio.run(_injects_skill_instructions_without_persisting_them())


async def _injects_skill_instructions_without_persisting_them():
    class CapturingProvider:
        def __init__(self):
            self.requests = []

        async def stream(self, request):
            self.requests.append(request)
            yield TextDelta(text="done")
            yield ModelCompleted()

    store, skills, provider = Store(), Skills(), CapturingProvider()
    kernel = Kernel(provider, store, Tools(), Settings(), skills=skills)
    session = await kernel.start()
    await kernel.submit("please use $reviewer")
    await kernel.wait_for_turn()

    assert skills.calls == [("instructions", session, "please use $reviewer")]
    messages = provider.requests[0].messages
    assert messages[0].role == "system"
    assert messages[0].content.startswith("You are Merlin")
    assert messages[1:] == [
        ChatMessage(role="system", content="Complete reviewer instructions."),
        ChatMessage(role="user", content="please use $reviewer"),
    ]
    assert store.messages[session] == [
        ChatMessage(role="user", content="please use $reviewer"),
        ChatMessage(role="assistant", content="done"),
    ]


def test_live_campaign_context_and_compaction_reach_console_without_truncating_store():
    asyncio.run(_live_campaign_context_and_compaction_reach_console())


async def _live_campaign_context_and_compaction_reach_console():
    class CapturingProvider:
        def __init__(self):
            self.requests = []
            self.capabilities = SimpleNamespace(context_window_tokens=32_768)

        async def stream(self, request):
            self.requests.append(request)
            yield TextDelta(text="done")
            yield ModelCompleted()

    async def campaign_context(session_id):
        return f"Active campaign campaign/one for {session_id}; use assessment_cancel."

    store, provider = Store(), CapturingProvider()
    kernel = Kernel(
        provider,
        store,
        Tools(),
        Settings(),
        campaign_context=campaign_context,
    )
    session = await kernel.start()
    for index in range(60):
        store.messages[session].append(
            ChatMessage(role="user", content=f"old-{index}:" + "x" * 2_000)
        )
    durable_count = len(store.messages[session])
    await kernel.submit("what is running?")
    await kernel.wait_for_turn()

    request = provider.requests[0]
    assert any("Active campaign campaign/one" in item.content for item in request.messages)
    assert any("conversation context compacted" in item.content for item in request.messages)
    assert request.messages[-1].content == "what is running?"
    assert len(store.messages[session]) == durable_count + 2


@pytest.mark.asyncio
async def test_kernel_compacts_on_token_window_not_character_threshold() -> None:
    class CapturingProvider:
        capabilities = SimpleNamespace(context_window_tokens=1_024)

        def __init__(self):
            self.requests = []

        async def stream(self, request):
            self.requests.append(request)
            yield TextDelta(text="done")
            yield ModelCompleted()

    store, provider = Store(), CapturingProvider()
    kernel = Kernel(provider, store, Tools(), Settings())
    session = await kernel.start()
    for index in range(60):
        store.messages[session].append(ChatMessage(role="user", content=f"old-{index}:xxxx"))
    durable_count = len(store.messages[session])

    await kernel.submit("continue")
    await kernel.wait_for_turn()

    # The token soft-utilization trigger (from the routed 1_024-token window)
    # compacts the provider request even though the raw character volume is
    # far below any fixed character threshold.
    assert any(
        "conversation context compacted" in item.content for item in provider.requests[0].messages
    )
    assert len(store.messages[session]) > durable_count


@pytest.mark.asyncio
async def test_kernel_never_compacts_on_characters_above_96_kib_under_token_budget() -> None:
    class WideProvider:
        capabilities = SimpleNamespace(context_window_tokens=1_000_000)

        def __init__(self):
            self.requests = []

        async def stream(self, request):
            self.requests.append(request)
            yield TextDelta(text="done")
            yield ModelCompleted()

    store, provider = Store(), WideProvider()
    kernel = Kernel(provider, store, Tools(), Settings())
    session = await kernel.start()
    # Well above the retired fixed 96 KiB character threshold, yet far under
    # the routed model's token budget: compaction must not trigger.
    for index in range(24):
        store.messages[session].append(
            ChatMessage(role="user", content=f"old-{index}:" + "x" * 5_000)
        )

    await kernel.submit("continue")
    await kernel.wait_for_turn()

    assert sum(len(item.content) for item in store.messages[session]) > 96 * 1_024
    assert not any(
        "conversation context compacted" in item.content for item in provider.requests[0].messages
    )


@pytest.mark.asyncio
async def test_kernel_uses_exact_routed_model_context_window() -> None:
    class RoutedProvider:
        capabilities = SimpleNamespace(context_window_tokens=1_024)

        def __init__(self):
            self.requests = []

        def capabilities_for_model(self, model):
            assert model == "test-model"
            return SimpleNamespace(context_window_tokens=1_000_000)

        async def stream(self, request):
            self.requests.append(request)
            yield TextDelta(text="done")
            yield ModelCompleted()

    store, provider = Store(), RoutedProvider()
    kernel = Kernel(provider, store, Tools(), Settings())
    session = await kernel.start()
    for index in range(60):
        store.messages[session].append(
            ChatMessage(role="user", content=f"old-{index}:" + "x" * 2_000)
        )

    await kernel.submit("continue")
    await kernel.wait_for_turn()

    assert not any(
        "conversation context compacted" in message.content
        for message in provider.requests[0].messages
    )


@pytest.mark.asyncio
async def test_kernel_warns_when_model_context_window_is_unknown() -> None:
    store, provider = Store(), Provider()
    kernel = Kernel(provider, store, Tools(), Settings())
    await kernel.start()

    await kernel.submit("hello")
    await kernel.wait_for_turn()

    warnings = [
        event
        for event in store.events
        if event.type == "warning" and event.payload.get("code") == "context_window_assumed"
    ]
    assert warnings
    assert "131072" in warnings[0].payload["text"]
    assert "conservative assumption" in warnings[0].payload["text"]


def test_skills_remain_optional():
    asyncio.run(_skills_remain_optional())


async def _skills_remain_optional():
    store = Store()
    kernel = Kernel(Provider(), store, Tools(), Settings())
    await kernel.start()
    await kernel.submit("/skills list")
    event = await kernel.next_event()
    while event.type != "error":
        event = await kernel.next_event()
    assert event.payload["text"] == "skill support is not configured"


class Mcp:
    def __init__(self):
        self.calls = []

    async def list_servers(self):
        self.calls.append(("list",))
        return [{"name": "local", "state": "running"}]

    async def add_server(self, arguments):
        self.calls.append(("add", *arguments))
        return {"name": arguments[0], "state": "running"}

    async def remove_server(self, name):
        self.calls.append(("remove", name))
        return {"name": name, "removed": True}

    async def check_server(self, name):
        self.calls.append(("check", name))
        return {"name": name, "state": "running"}

    async def restart_server(self, name):
        self.calls.append(("restart", name))
        return {"name": name, "state": "running"}

    async def disable_server(self, name):
        self.calls.append(("disable", name))
        return {"name": name, "state": "disabled"}

    async def admit_server(self, name, digest):
        self.calls.append(("admit", name, digest))
        return {"name": name, "admission": "admitted"}


def test_routes_mcp_commands_and_forwards_add_arguments_opaquely():
    asyncio.run(_routes_mcp_commands_and_forwards_add_arguments_opaquely())


async def _routes_mcp_commands_and_forwards_add_arguments_opaquely():
    store, mcp = Store(), Mcp()
    kernel = Kernel(Provider(), store, Tools(), Settings(), mcp=mcp)
    await kernel.start()
    await kernel.submit("/mcp list")
    await kernel.submit('/mcp add local --env "TOKEN=secret value" python server.py')
    await kernel.submit(
        "/mcp add remote --remote https://mcp.test/mcp --credential remote-prod "
        "--transport streamable_http"
    )
    await kernel.submit("/mcp check local")
    await kernel.submit("/mcp restart local")
    await kernel.submit("/mcp disable local")
    await kernel.submit("/mcp admit remote abc123")
    await kernel.submit("/mcp remove local")

    assert mcp.calls == [
        ("list",),
        ("add", "local", "--env", "TOKEN=secret value", "python", "server.py"),
        (
            "add",
            "remote",
            "--remote",
            "https://mcp.test/mcp",
            "--credential",
            "remote-prod",
            "--transport",
            "streamable_http",
        ),
        ("check", "local"),
        ("restart", "local"),
        ("disable", "local"),
        ("admit", "remote", "abc123"),
        ("remove", "local"),
    ]
    command_text = "\n".join(
        str(event.payload.get("text", "")) for event in store.events if event.type == "command"
    )
    assert "secret value" not in command_text


def test_mcp_errors_redact_authentication_material():
    asyncio.run(_mcp_errors_redact_authentication_material())


async def _mcp_errors_redact_authentication_material():
    class FailingMcp(Mcp):
        async def check_server(self, name):
            raise RuntimeError("Authorization: Bearer top-secret-token")

    store = Store()
    kernel = Kernel(Provider(), store, Tools(), Settings(), mcp=FailingMcp())
    await kernel.start()
    await kernel.submit("/mcp check remote")
    text = store.events[-1].payload["text"]
    assert "top-secret-token" not in text
    assert "[REDACTED]" in text


def test_mcp_errors_are_emitted_and_service_is_optional():
    asyncio.run(_mcp_errors_are_emitted_and_service_is_optional())


async def _mcp_errors_are_emitted_and_service_is_optional():
    store = Store()
    kernel = Kernel(Provider(), store, Tools(), Settings())
    await kernel.start()
    await kernel.submit("/mcp list")
    await kernel.submit("/mcp restart")

    errors = [event.payload["text"] for event in store.events if event.type == "error"]
    assert errors == ["MCP support is not configured", "MCP support is not configured"]

    store, mcp = Store(), Mcp()
    kernel = Kernel(Provider(), store, Tools(), Settings(), mcp=mcp)
    await kernel.start()
    await kernel.submit("/mcp restart")
    assert store.events[-1].type == "error"
    assert store.events[-1].payload["text"].startswith("usage: /mcp")


class Promotions:
    def __init__(self):
        self.calls = []

    async def propose(self, session_id, arguments):
        self.calls.append(("propose", session_id, *arguments))
        return {"id": "candidate-9"}

    async def list_candidates(self):
        self.calls.append(("list",))
        return [{"id": "candidate-9"}]

    async def show_candidate(self, candidate_id):
        self.calls.append(("show", candidate_id))
        return {"id": candidate_id, "exact_diff": "+ profile"}

    async def evaluate(self, session_id, candidate_id, suite_id):
        self.calls.append(("evaluate", session_id, candidate_id, suite_id))
        return {"candidate_id": candidate_id, "passed": True}

    async def accept(self, candidate_id, reason):
        self.calls.append(("accept", candidate_id, reason))
        return {"candidate_id": candidate_id, "status": "accepted"}

    async def reject(self, candidate_id, reason):
        return {"candidate_id": candidate_id, "status": "rejected"}

    async def activate(self, candidate_id):
        self.calls.append(("activate", candidate_id))
        return {"candidate_id": candidate_id, "status": "active"}

    async def history(self, kind):
        self.calls.append(("history", kind))
        return []

    async def rollback(self, kind):
        self.calls.append(("rollback", kind))
        return {"kind": kind, "status": "rolled_back"}


def test_routes_operator_promotion_lifecycle_commands():
    asyncio.run(_routes_operator_promotion_lifecycle_commands())


async def _routes_operator_promotion_lifecycle_commands():
    store, promotions = Store(), Promotions()
    kernel = Kernel(Provider(), store, Tools(), Settings(), promotions=promotions)
    session = await kernel.start()
    await kernel.submit("/promotion list")
    await kernel.submit("/promotion show candidate-9")
    await kernel.submit("/promotion evaluate candidate-9 frozen-v1")
    await kernel.submit('/promotion accept candidate-9 "reviewed passing suite"')
    await kernel.submit("/promotion activate candidate-9")
    await kernel.submit("/promotion history profile")
    await kernel.submit("/promotion rollback profile")
    await kernel.submit('/promotion propose "{\\"candidate\\":{},\\"evidence\\":{}}"')

    assert promotions.calls == [
        ("list",),
        ("show", "candidate-9"),
        ("evaluate", session, "candidate-9", "frozen-v1"),
        ("accept", "candidate-9", "reviewed passing suite"),
        ("activate", "candidate-9"),
        ("history", "profile"),
        ("rollback", "profile"),
        ("propose", session, '{"candidate":{},"evidence":{}}'),
    ]


class Agents:
    def __init__(self):
        self.calls = []
        self.requests = []

    async def list_agents(self, session_id):
        self.calls.append(("agents", session_id))
        return [AgentSummary(id="a1", name="worker", status="running")]

    async def list_tasks(self, session_id):
        self.calls.append(("tasks", session_id))
        return [AgentTask(id="t1", objective="check", status="queued")]

    async def start_swarm(self, request):
        self.requests.append(request)
        yield AgentProgress(kind="agent", state="started", agent_id="a1")
        yield AgentProgress(kind="task", state="completed", task_id="t1", text="done")

    async def stop_swarm(self, session_id):
        self.calls.append(("stop_swarm", session_id))

    async def deliver_message(self, request):
        self.calls.append(
            ("message", request.session_id, request.recipient_agent_id, request.message)
        )
        return MailboxDelivery(
            message_id="m1",
            schedule_id="schedule-1",
            recipient_agent_id=request.recipient_agent_id,
            state="queued",
        )

    async def stop_agent(self, session_id, agent_id):
        self.calls.append(("stop_agent", session_id, agent_id))


def test_routes_agent_commands_and_emits_typed_progress():
    asyncio.run(_routes_agent_commands_and_emits_typed_progress())


async def _routes_agent_commands_and_emits_typed_progress():
    store, agents = Store(), Agents()
    kernel = Kernel(Provider(), store, Tools(), Settings(), agents=agents)
    session = await kernel.start()
    await kernel.submit("/agents")
    await kernel.submit("/tasks")
    await kernel.submit('/agent message a1 "focus tests"')
    await kernel.submit("/agent stop a1")

    assert agents.requests == []
    assert agents.calls == [
        ("agents", session),
        ("tasks", session),
        ("message", session, "a1", "focus tests"),
        ("stop_agent", session, "a1"),
    ]
    progress = [event for event in store.events if event.type == "agent_progress"]
    assert [(event.payload["kind"], event.payload["state"]) for event in progress] == [
        ("mailbox", "queued"),
        ("agent", "stop_requested"),
    ]


def test_agent_service_is_optional_and_worker_limit_is_bounded():
    asyncio.run(_agent_service_is_optional())
    with pytest.raises(ValueError, match="max_workers"):
        Kernel(Provider(), Store(), Tools(), Settings(), max_workers=4)


async def _agent_service_is_optional():
    store = Store()
    kernel = Kernel(Provider(), store, Tools(), Settings())
    await kernel.start()
    await kernel.submit("/agents")
    assert store.events[-1].payload["text"] == "agent support is not configured"


class Findings:
    def __init__(self):
        self.calls = []
        self.finding = Finding(
            id="f1",
            title="Missing CSP",
            severity="medium",
            summary="No policy.",
            reproduction="Inspect headers.",
            remediation="Add CSP.",
            creator="agent:a1",
            evidence_references=["event:e1"],
        )

    async def list_findings(self, session_id):
        self.calls.append(("list", session_id))
        return [self.finding]

    async def accept_finding(self, session_id, finding_id):
        self.calls.append(("accept", session_id, finding_id))
        return self.finding.model_copy(update={"status": "accepted"})

    async def reject_finding(self, session_id, finding_id):
        self.calls.append(("reject", session_id, finding_id))
        return self.finding.model_copy(update={"status": "rejected"})

    async def generate_report(self, request):
        self.calls.append(("report", request))
        return ReportResult(format=request.format, output=request.output, finding_count=1)

    async def compare_reports(self, older_id, newer_id):
        self.calls.append(("compare", older_id, newer_id))
        return ("$.findings: 1 -> 2",)


def test_routes_findings_and_report_with_quoted_output_path():
    asyncio.run(_routes_findings_and_report_with_quoted_output_path())


async def _routes_findings_and_report_with_quoted_output_path():
    store, findings = Store(), Findings()
    kernel = Kernel(Provider(), store, Tools(), Settings(), findings=findings)
    session = await kernel.start()
    await kernel.submit("/findings")
    await kernel.submit("/findings accept f1")
    await kernel.submit("/findings reject f1")
    await kernel.submit('/report run --output "reports/my report.md" --format markdown')
    await kernel.submit("/report compare revision-1 revision-2")

    assert findings.calls[:3] == [
        ("list", session),
        ("accept", session, "f1"),
        ("reject", session, "f1"),
    ]
    request = findings.calls[3][1]
    assert request.output.as_posix() == "reports/my report.md"
    assert request.format == "markdown"
    report_events = [event for event in store.events if event.type == "report"]
    assert [event.payload["state"] for event in report_events] == ["started", "completed"]
    assert report_events[-1].payload["finding_count"] == 1
    assert findings.calls[-1] == ("compare", "revision-1", "revision-2")
    comparison = next(
        event
        for event in store.events
        if event.type == "command" and event.payload["command"] == "report_comparison"
    )
    assert '"older": "revision-1"' in comparison.payload["text"]
    assert '"newer": "revision-2"' in comparison.payload["text"]
    assert '"$.findings: 1 -> 2"' in comparison.payload["text"]


def test_findings_are_optional_and_report_usage_is_strict():
    asyncio.run(_findings_are_optional_and_report_usage_is_strict())


def test_report_default_output_has_utc_datetime_prefix():
    asyncio.run(_report_default_output_has_utc_datetime_prefix())


async def _report_default_output_has_utc_datetime_prefix():
    store, findings = Store(), Findings()
    kernel = Kernel(Provider(), store, Tools(), Settings(), findings=findings)
    await kernel.start()
    await kernel.submit("/report run --format csv")

    request = findings.calls[-1][1]
    assert re.fullmatch(r"reports/\d{8}T\d{6}Z-merlin-report\.zip", request.output.as_posix())


async def _findings_are_optional_and_report_usage_is_strict():
    store = Store()
    kernel = Kernel(Provider(), store, Tools(), Settings())
    await kernel.start()
    await kernel.submit("/findings")
    assert store.events[-1].payload["text"] == "finding support is not configured"

    store, findings = Store(), Findings()
    kernel = Kernel(Provider(), store, Tools(), Settings(), findings=findings)
    await kernel.start()
    await kernel.submit("/report --format html --audience customer --output report.html")
    await kernel.submit("/report --format json --output one --output two")
    errors = [event.payload["text"] for event in store.events if event.type == "error"]
    assert errors == [
        "usage: /report [run] [--format html|markdown|csv|json] "
        "[--audience internal|customer] [--output PATH]"
        " | /report compare OLDER_REVISION NEWER_REVISION",
    ]
    assert findings.calls[-1][0] == "report"
    assert findings.calls[-1][1].format == "html"
    assert findings.calls[-1][1].audience == "customer"


class Browsers:
    def __init__(self):
        self.calls = []

    async def list_contexts(self, session_id):
        self.calls.append(("contexts", session_id))
        return [{"context_id": "ctx1", "state": "restart_required"}]

    async def list_tabs(self, session_id, context_id):
        self.calls.append(("tabs", session_id, context_id))
        return [{"tab_ref": "tab1", "url": "https://target.test"}]

    async def takeover(self, session_id, context_id):
        self.calls.append(("takeover", session_id, context_id))
        return {"context_id": context_id, "worker_id": "operator"}

    async def cleanup(self, session_id, context_id):
        self.calls.append(("cleanup", session_id, context_id))
        return {"context_id": context_id, "state": "cleaned"}

    async def resume(self, session_id, context_id):
        self.calls.append(("resume", session_id, context_id))
        return {"context_id": context_id, "state": "restart_required"}


def test_routes_browser_lifecycle_commands_and_keeps_service_optional():
    asyncio.run(_routes_browser_lifecycle_commands_and_keeps_service_optional())


async def _routes_browser_lifecycle_commands_and_keeps_service_optional():
    store, browsers = Store(), Browsers()
    kernel = Kernel(Provider(), store, Tools(), Settings(), browser=browsers)
    session = await kernel.start()
    await kernel.submit("/browser contexts")
    await kernel.submit("/browser tabs ctx1")
    await kernel.submit("/browser takeover ctx1")
    await kernel.submit("/browser cleanup ctx1")
    await kernel.submit("/browser resume ctx1")
    assert browsers.calls == [
        ("contexts", session),
        ("tabs", session, "ctx1"),
        ("takeover", session, "ctx1"),
        ("cleanup", session, "ctx1"),
        ("resume", session, "ctx1"),
    ]
    assert [event.payload["action"] for event in store.events if event.type == "browser"] == [
        "takeover",
        "cleanup",
        "resume",
    ]

    optional_store = Store()
    optional = Kernel(Provider(), optional_store, Tools(), Settings())
    await optional.start()
    await optional.submit("/browser contexts")
    assert optional_store.events[-1].payload["text"] == "browser support is not configured"


class Memory:
    def __init__(self):
        self.calls = []

    async def list_memory(self, status=None):
        self.calls.append(("list", status))
        return [{"record_id": "lesson/1", "status": status or "accepted"}]

    async def import_file(self, path):
        self.calls.append(("import", path))
        return {"status": "quarantined"}

    async def export_file(self, path):
        self.calls.append(("export", path))
        return {"path": path}

    async def accept(self, bundle_id, record_ids):
        self.calls.append(("accept", bundle_id, tuple(record_ids)))
        return {"accepted": list(record_ids)}

    async def reject(self, bundle_id, record_ids, reason):
        self.calls.append(("reject", bundle_id, tuple(record_ids), reason))
        return {"rejected": list(record_ids)}

    async def delete(self, bundle_id, record_ids, reason):
        self.calls.append(("delete", bundle_id, tuple(record_ids), reason))
        return {"deleted": list(record_ids)}


def test_routes_portable_memory_lifecycle_and_keeps_service_optional():
    asyncio.run(_routes_portable_memory_lifecycle_and_keeps_service_optional())


async def _routes_portable_memory_lifecycle_and_keeps_service_optional():
    store, memory = Store(), Memory()
    kernel = Kernel(Provider(), store, Tools(), Settings(), memory=memory)
    await kernel.start()
    await kernel.submit("/memory list quarantined")
    await kernel.submit("/memory import bundles/reviewed.json")
    await kernel.submit("/memory export bundles/export.json")
    await kernel.submit("/memory accept bundle/1 lesson/1 lesson/2")
    await kernel.submit('/memory reject bundle/1 lesson/3 "unsupported evidence"')
    await kernel.submit('/memory delete bundle/1 lesson/4 "operator cleanup"')

    assert memory.calls == [
        ("list", "quarantined"),
        ("import", "bundles/reviewed.json"),
        ("export", "bundles/export.json"),
        ("accept", "bundle/1", ("lesson/1", "lesson/2")),
        ("reject", "bundle/1", ("lesson/3",), "unsupported evidence"),
        ("delete", "bundle/1", ("lesson/4",), "operator cleanup"),
    ]

    optional_store = Store()
    optional = Kernel(Provider(), optional_store, Tools(), Settings())
    await optional.start()
    await optional.submit("/memory list")
    assert optional_store.events[-1].payload["text"] == "portable memory is not configured"


def test_request_input_budget_subtracts_explicit_reservations() -> None:
    from merlin_kernel import request_input_budget

    # Window minus output reserve, request overhead (tool schemas, provider
    # continuation metadata), and the safety margin.
    assert (
        request_input_budget(10_000, reserved_output_tokens=2_000, reserved_overhead_tokens=500)
        == 10_000 - 2_000 - 500 - 256
    )
    # Reservations exceeding a small window clamp to a well-defined floor.
    assert request_input_budget(1_024, reserved_output_tokens=8_192) == 1


@pytest.mark.asyncio
async def test_kernel_route_change_takes_effect_on_the_next_turn() -> None:
    windows = {"big-model": 1_000_000, "small-model": 16_384}

    class RoutedProvider:
        def __init__(self):
            self.requests = []

        def capabilities_for_model(self, model):
            return SimpleNamespace(context_window_tokens=windows[model])

        async def stream(self, request):
            self.requests.append(request)
            yield TextDelta(text="done")
            yield ModelCompleted()

    store, provider = Store(), RoutedProvider()
    settings = Settings()
    settings.model = "big-model"
    kernel = Kernel(provider, store, Tools(), settings)
    session = await kernel.start()
    for index in range(60):
        store.messages[session].append(
            ChatMessage(role="user", content=f"old-{index}:" + "x" * 2_000)
        )

    await kernel.submit("first")
    await kernel.wait_for_turn()
    assert provider.requests[0].model == "big-model"
    assert not any(
        "conversation context compacted" in message.content
        for message in provider.requests[0].messages
    )

    # Saving a model change mid-session must recalculate the budget against the
    # newly routed model's own window on the very next turn, without a restart.
    settings.model = "small-model"
    await kernel.submit("second")
    await kernel.wait_for_turn()

    request = provider.requests[1]
    assert request.model == "small-model"
    assert any("conversation context compacted" in message.content for message in request.messages)


@pytest.mark.asyncio
async def test_hung_tool_produces_single_terminal_failure() -> None:
    class HungToolProvider:
        def __init__(self):
            self.requests = 0

        async def stream(self, request):
            self.requests += 1
            if self.requests == 1:
                yield ToolCall(call_id="call-a", name="read", arguments={"path": "a"})
            else:
                yield TextDelta(text="done")
                yield ModelCompleted()

    class HungTools(Tools):
        def describe(self):
            return (ToolSpec(name="read", description="read", input_schema={"type": "object"}),)

        async def invoke(self, name, context, arguments):
            await asyncio.Event().wait()
            return ToolResult(content="never")

    store = Store()
    kernel = Kernel(HungToolProvider(), store, HungTools(), Settings(), tool_timeout_seconds=0.05)
    session = await kernel.start()
    await kernel.submit("go")
    await kernel.wait_for_turn()

    tool_messages = [m for m in store.messages[session] if m.role == "tool"]
    assert len(tool_messages) == 1
    assert tool_messages[0].tool_call_id == "call-a"
    assert "timed out" in tool_messages[0].content
    assert any(m.role == "assistant" and m.content == "done" for m in store.messages[session])


def test_tool_timeout_must_be_positive() -> None:
    with pytest.raises(ValueError, match="tool timeout"):
        Kernel(Provider(), Store(), Tools(), Settings(), tool_timeout_seconds=0)


@pytest.mark.asyncio
async def test_provider_failure_event_uses_typed_infrastructure_category() -> None:
    class MissingPointerProvider:
        async def stream(self, request):
            yield ModelError(
                message="configured response pointer was absent",
                code="missing_response_pointer",
            )

    store = Store()
    kernel = Kernel(MissingPointerProvider(), store, Tools(), Settings())
    await kernel.start()
    await kernel.submit("go")
    await kernel.wait_for_turn()

    event = next(item for item in store.events if item.type == "error")
    assert event.payload["failure_class"] == "infrastructure"
    assert (
        event.payload["infrastructure_failure_category"]
        == InfrastructureFailureCategory.MISSING_RESPONSE_POINTER.value
    )


@pytest.mark.asyncio
async def test_kernel_usage_event_preserves_cache_token_partition() -> None:
    class UsageProvider:
        async def stream(self, request):
            yield ModelUsage(
                input_tokens=17,
                output_tokens=3,
                total_tokens=20,
                uncached_input_tokens=8,
                cache_read_input_tokens=6,
                cache_creation_input_tokens=3,
                cache_hit=True,
                cache_created=True,
            )
            yield TextDelta(text="done")
            yield ModelCompleted()

    store = Store()
    kernel = Kernel(UsageProvider(), store, Tools(), Settings())
    await kernel.start()
    await kernel.submit("go")
    await kernel.wait_for_turn()

    payload = next(item.payload for item in store.events if item.type == "usage")
    assert payload["uncached_input_tokens"] == 8
    assert payload["cache_read_input_tokens"] == 6
    assert payload["cache_creation_input_tokens"] == 3
    assert payload["input_tokens"] == 17


@pytest.mark.asyncio
async def test_tool_timeout_event_uses_typed_infrastructure_category() -> None:
    class ToolProvider:
        async def stream(self, request):
            if any(message.role == "tool" for message in request.messages):
                yield TextDelta(text="done")
                yield ModelCompleted()
                return
            yield ToolCall(call_id="call-a", name="read", arguments={"path": "a"})

    class HungTools(Tools):
        def describe(self):
            return (ToolSpec(name="read", description="read", input_schema={"type": "object"}),)

        async def invoke(self, name, context, arguments):
            await asyncio.Event().wait()

    store = Store()
    kernel = Kernel(ToolProvider(), store, HungTools(), Settings(), tool_timeout_seconds=0.01)
    await kernel.start()
    await kernel.submit("go")
    await kernel.wait_for_turn()

    event = next(
        item
        for item in store.events
        if item.type == "tool" and item.payload.get("state") == "failed"
    )
    assert event.payload["failure_class"] == "infrastructure"
    assert (
        event.payload["infrastructure_failure_category"]
        == InfrastructureFailureCategory.ADAPTER_TIMEOUT.value
    )


@pytest.mark.asyncio
async def test_orphan_tool_results_dropped_from_provider_replay_but_kept_durable() -> None:
    class CapturingProvider:
        def __init__(self):
            self.requests = []

        async def stream(self, request):
            self.requests.append(request)
            yield TextDelta(text="done")
            yield ModelCompleted()

    store, provider = Store(), CapturingProvider()
    session = await store.create_session()
    store.messages[session].extend(
        [
            ChatMessage(role="user", content="earlier"),
            ChatMessage(role="tool", content="ghost result", name="read", tool_call_id="ghost-1"),
            ChatMessage(
                role="assistant",
                content="",
                tool_calls=[
                    {
                        "id": "paired-1",
                        "type": "function",
                        "function": {"name": "read", "arguments": "{}"},
                    }
                ],
            ),
            ChatMessage(role="tool", content="paired result", name="read", tool_call_id="paired-1"),
        ]
    )
    kernel = Kernel(provider, store, Tools(), Settings())
    await kernel.start(session)

    await kernel.submit("continue")
    await kernel.wait_for_turn()

    replay_ids = [
        message.tool_call_id for message in provider.requests[0].messages if message.role == "tool"
    ]
    # The orphan result is dropped from replay; the paired result is kept.
    assert replay_ids == ["paired-1"]
    # The durable transcript is untouched: the orphan result is still stored.
    assert any(
        message.role == "tool" and message.tool_call_id == "ghost-1"
        for message in store.messages[session]
    )


@pytest.mark.asyncio
async def test_orphan_tool_results_dropped_with_context_loader_projection() -> None:
    from merlin_contracts import ContextProjection, MessageLineage
    from merlin_kernel import MERLIN_IDENTITY, digest_messages

    class CapturingProvider:
        def __init__(self):
            self.requests = []

        async def stream(self, request):
            self.requests.append(request)
            yield TextDelta(text="done")
            yield ModelCompleted()

    class ProjectionStore(Store):
        async def load_context(self, session_id, identity):
            messages = tuple(self.messages[session_id])
            return ContextProjection(
                session_id=session_id,
                identity=identity,
                messages=messages,
                lineage=tuple(
                    MessageLineage(
                        sequence=index + 1,
                        message_id=f"m{index}",
                        source_message_id=f"m{index}",
                    )
                    for index in range(len(messages))
                ),
                transcript_digest=digest_messages(messages),
                head_sequence=len(messages),
            )

    store, provider = ProjectionStore(), CapturingProvider()
    session = await store.create_session()
    store.messages[session].extend(
        [
            ChatMessage(role="user", content="earlier"),
            ChatMessage(role="tool", content="ghost result", name="read", tool_call_id="ghost-2"),
        ]
    )
    kernel = Kernel(provider, store, Tools(), Settings())
    await kernel.start(session)

    await kernel.submit("continue")
    await kernel.wait_for_turn()

    assert MERLIN_IDENTITY.role == "system"
    replay_ids = [
        message.tool_call_id for message in provider.requests[0].messages if message.role == "tool"
    ]
    assert "ghost-2" not in replay_ids
    assert any(
        message.role == "tool" and message.tool_call_id == "ghost-2"
        for message in store.messages[session]
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("dynamic", [False, True])
async def test_structured_question_pauses_then_continues_from_exact_answer(dynamic):
    from merlin_contracts.user_input import (
        InputRequest,
        InputResponse,
        answer_prompt,
        pending_input,
    )

    request_value = InputRequest(
        request_id="q1",
        questions=[
            {
                "id": "auth",
                "question": "Which authentication path?",
                "options": [
                    {"id": "saved", "label": "Saved session"},
                    {"id": "browser", "label": "Browser sign-in"},
                ],
            }
        ],
    )
    calls, invoked = [], []

    class QuestionTools(Tools):
        def describe(self):
            return (
                ToolSpec(
                    name="request_user_input",
                    description="Ask operator",
                    input_schema={"type": "object"},
                ),
                ToolSpec(
                    name="must_not_run", description="Must wait", input_schema={"type": "object"}
                ),
            )

        async def invoke(self, name, context, arguments):
            invoked.append(name)
            value = {"status": "awaiting_input", "request": request_value.model_dump()}
            return ToolResult(content=json.dumps(value), metadata=value)

    class QuestionProvider:
        async def stream(self, request):
            calls.append(request)
            if len(calls) == 1:
                yield ToolCall(name="request_user_input", call_id="q1", arguments={})
                yield ToolCall(name="must_not_run", call_id="deferred", arguments={})
            else:
                assert '"saved"' in request.messages[-1].content
                yield TextDelta(text="Continuing with the saved session.")
            yield ModelCompleted()

    class DynamicQuestionProvider(QuestionProvider):
        async def stream_with_tools(self, request, handler):
            calls.append(request)
            if len(calls) == 1:
                await handler(ToolCall(name="request_user_input", call_id="q1", arguments={}))
                pytest.fail("Provider must stop when question is saved")
            else:
                assert '"saved"' in request.messages[-1].content
                yield TextDelta(text="Continuing with the saved session.")
                yield ModelCompleted()

    store = Store()
    kernel = Kernel(
        DynamicQuestionProvider() if dynamic else QuestionProvider(),
        store,
        QuestionTools(),
        Settings(),
    )
    await kernel.start()
    await kernel.submit("Inspect the existing target")
    await kernel.wait_for_turn()
    assert len(calls) == 1 and invoked == ["request_user_input"]
    saved = await store.load_messages(kernel.session_id)
    pending = pending_input(saved)
    assert pending == request_value
    # All batched calls have results, including the explicitly deferred call.
    call_ids = {call["id"] for message in saved for call in message.tool_calls or []}
    assert call_ids == {message.tool_call_id for message in saved if message.role == "tool"}
    response = InputResponse(request_id="q1", answers={"auth": {"option_ids": ["saved"]}})
    await kernel.submit(answer_prompt(pending, response))
    await kernel.wait_for_turn()
    assert len(calls) == 2
    assert pending_input(await store.load_messages(kernel.session_id)) is None
    assert not any(event.type == "error" for event in store.events)
