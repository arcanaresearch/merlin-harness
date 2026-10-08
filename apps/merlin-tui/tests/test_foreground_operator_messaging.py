import json

from merlin_config import Config, CredentialStore
from merlin_contracts import ModelCompleted, TextDelta, ToolCall, ToolContext
from merlin_permissions import PermissionDecision
from merlin_storage_postgres import Database, DatabaseConfig
from merlin_tui.bootstrap import _INTERNAL_NON_CAMPAIGN_TOOL_IDS, build_backend


class ForwardingProvider:
    def __init__(self):
        self.requests = []
        self.receipts = []

    async def stream(self, request):
        self.requests.append(request)
        names = {tool["function"]["name"] for tool in request.tools}
        assert {"campaign_message", "agent_message", "agent_list"} <= names
        if len(self.requests) == 1:
            assert any(
                message.role == "user" and "prioritize authentication" in message.content
                for message in request.messages
            )
            yield ToolCall(
                call_id="operator-guidance-1",
                name="campaign_message",
                arguments={
                    "campaign_id": "campaign/forwarding",
                    "message": "prioritize authentication",
                },
            )
            yield ModelCompleted(finish_reason="tool_calls")
        else:
            receipt = next(
                message for message in reversed(request.messages) if message.role == "tool"
            )
            self.receipts.append(json.loads(receipt.content))
            assert self.receipts[-1]["delivery_status"] == "queued"
            yield TextDelta(text="Queued for the Lead's next decision boundary.")
            yield ModelCompleted()


async def test_production_foreground_routes_chat_to_durable_lead_guidance(tmp_path, monkeypatch):
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
            provider = ForwardingProvider()
            backend.kernel._provider = provider

            async def campaign_context(session_id):
                return "Active campaign: campaign/forwarding. Lead guidance is queued."

            backend.kernel._campaign_context = campaign_context
            session_id = await backend.kernel.start()
            campaign_store = backend._campaigns._store
            campaign_store.prepare("campaign/forwarding", session_id, {}, "sha256:reviewed")
            await backend.kernel.submit("Tell the lead to prioritize authentication")
            await backend.kernel.wait_for_turn()
            assert len(provider.requests) == 2
            assert provider.receipts[0]["recipient"] == "campaign_lead"
            entries = campaign_store.list_lead("campaign/forwarding")
            guidance = [row for row in entries if row["payload"].get("kind") == "operator_guidance"]
            assert len(guidance) == 1
            assert guidance[0]["payload"]["message"] == "prioritize authentication"

            # Replay through the real broker/storage must retain a single durable row.
            replay = await backend.kernel._tools.invoke(
                "campaign_message",
                ToolContext(
                    session_id=session_id,
                    workspace_root=str(tmp_path),
                    call_id="operator-guidance-1",
                ),
                {"campaign_id": "campaign/forwarding", "message": "prioritize authentication"},
            )
            assert not replay.is_error
            assert replay.metadata["guidance_id"] == provider.receipts[0]["guidance_id"]
            service_replay = await backend._campaigns.guide(
                session_id,
                "campaign/forwarding",
                "prioritize authentication",
                request_id="operator-guidance-1",
            )
            assert service_replay["guidance_id"] == replay.metadata["guidance_id"]
            assert len(campaign_store.list_lead("campaign/forwarding")) == 1
            assert {
                "campaign_message",
                "agent_message",
                "agent_list",
            } <= _INTERNAL_NON_CAMPAIGN_TOOL_IDS
        finally:
            await backend.close()
