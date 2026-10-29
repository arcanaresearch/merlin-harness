import json
from unittest.mock import AsyncMock

from merlin_config import Config, CredentialStore
from merlin_contracts import ChatMessage, ModelCompleted, TextDelta, ToolCall, ToolResult
from merlin_permissions import PermissionDecision
from merlin_storage_postgres import Database, DatabaseConfig
from merlin_tui.bootstrap import _INTERNAL_NON_CAMPAIGN_TOOL_IDS, build_backend


async def test_chat_intent_survives_real_kernel_storage_and_rejects_old_approval(
    tmp_path, monkeypatch
):
    with Database(DatabaseConfig(data_dir=tmp_path / "postgres")) as database:
        monkeypatch.setattr(
            "merlin_config.load_config",
            lambda: Config(
                database_url=database.url,
                values={"provider": {"base_url": "http://127.0.0.1:9/v1", "model": "test"}},
            ),
        )
        monkeypatch.setattr(CredentialStore, "load", lambda self: {"api_key": ""})

        async def confirm(*_):
            return PermissionDecision.ALLOW_SESSION

        backend = build_backend(tmp_path / "workspace", tool_confirmer=confirm)
        try:
            kernel = backend.kernel
            session_id = await kernel.start()
            kernel._campaign_context = AsyncMock(return_value="Older L4 review is prepared.")
            await kernel._store.append_message(
                session_id,
                ChatMessage(
                    role="assistant",
                    content="",
                    tool_calls=[
                        {
                            "id": "old-review",
                            "type": "function",
                            "function": {
                                "name": "assessment_prepare",
                                "arguments": "{}",
                            },
                        }
                    ],
                ),
            )
            await kernel._store.append_message(
                session_id,
                ChatMessage(
                    role="tool",
                    name="assessment_prepare",
                    tool_call_id="old-review",
                    content=json.dumps(
                        {"campaign_id": "campaign/old-L4", "manifest_digest": "sha256:old"}
                    ),
                ),
            )
            tools = kernel._tools._tools
            tools["campaign_configure"]._preflight = AsyncMock(return_value=(True, "ready"))
            tools["campaign_configure"]._activate = None
            prepared = {
                "campaign_id": "campaign/new-L3",
                "manifest_digest": "sha256:new",
                "status": "prepared",
                "approval_required": True,
                "review": {
                    "profile": "exploration",
                    "objective": "Discovery scan on L3",
                    "large_manifest": "x" * 100_000,
                },
            }
            prepare = AsyncMock(
                return_value=ToolResult(
                    content=json.dumps(prepared),
                    metadata=prepared,
                )
            )
            recall = AsyncMock(return_value=ToolResult(content=json.dumps({"status": "recalled"})))
            execute = AsyncMock()
            tools["browser_recall_reconnaissance"].invoke = recall
            tools["assessment_prepare"]._tool.invoke = prepare
            tools["assessment_execute"]._tool.invoke = execute

            class Provider:
                count = 0

                async def stream(self, request):
                    self.count += 1
                    if self.count == 1:
                        yield ToolCall(
                            call_id="intent",
                            name="campaign_configure",
                            arguments={
                                "new_campaign": True,
                                "objective": "Discovery scan on L3",
                                "campaign_profile": "exploration",
                                "execution_mode": "hybrid",
                                "specialist_composition": "autonomous",
                            },
                        )
                    elif self.count == 2:
                        yield ToolCall(
                            call_id="held-review",
                            name="assessment_prepare",
                            arguments={
                                "objective": "Old L4",
                                "campaign_profile": "quickscan",
                                "adapter": {"recon_handoff_id": "new-sealed-handoff"},
                            },
                        )
                    elif self.count == 3:
                        yield ToolCall(
                            call_id="stale-approve",
                            name="assessment_execute",
                            arguments={
                                "campaign_id": "campaign/old-L4",
                                "manifest_digest": "sha256:old",
                            },
                        )
                    elif self.count == 4:
                        assert "browser_recall_reconnaissance" in {
                            tool["function"]["name"] for tool in request.tools
                        }
                        yield ToolCall(
                            call_id="recall-recon",
                            name="browser_recall_reconnaissance",
                            arguments={"surface": "L3", "agent_or_bot": "discovery scan"},
                        )
                    elif self.count == 5:
                        yield ToolCall(
                            call_id="new-review",
                            name="assessment_prepare",
                            arguments={
                                "objective": "Old L4",
                                "campaign_profile": "quickscan",
                                "adapter": {"recon_handoff_id": "new-sealed-handoff"},
                            },
                        )
                    else:
                        yield TextDelta(text="The L3 exploration campaign is ready for review.")
                        yield ModelCompleted()
                        return
                    yield ModelCompleted(finish_reason="tool_calls")

            provider = Provider()
            kernel._provider = provider
            events = [event async for event in backend.stream("Run a new discovery scan on L3")]
            assert not [event.text for event in events if event.kind == "error"]
            assert provider.count == 6
            recall.assert_awaited_once()
            arguments = prepare.call_args.args[1]
            assert arguments["objective"] == "Discovery scan on L3"
            assert arguments["campaign_profile"] == "exploration"
            assert arguments["adapter"]["execution_mode"] == "hybrid"
            assert arguments["specialist_composition"] == "autonomous"
            execute.assert_not_awaited()
            review = next(event for event in events if event.kind == "campaign_review")
            assert json.loads(review.text)["campaign_id"] == "campaign/new-L3"
            messages = await kernel._store.load_messages(session_id)
            held_receipt = next(m for m in messages if m.tool_call_id == "held-review")
            assert "not admitted" in held_receipt.content
            receipt = next(m for m in messages if m.tool_call_id == "new-review")
            assert (
                json.JSONDecoder().raw_decode(receipt.content)[0]["manifest_digest"] == "sha256:new"
            )
            assert len(receipt.content) < 1000
            stale_receipt = next(m for m in messages if m.tool_call_id == "stale-approve")
            assert "not admitted" in stale_receipt.content
            names = {spec.name for spec in kernel._tools.describe()}
            assert {
                "campaign_configure",
                "operator_command",
                "operator_runtime",
                "operator_inspect",
            } <= names
            assert {
                "campaign_configure",
                "operator_command",
                "operator_runtime",
                "operator_inspect",
            } <= _INTERNAL_NON_CAMPAIGN_TOOL_IDS
        finally:
            await backend.close()
