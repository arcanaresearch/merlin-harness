import json
from types import SimpleNamespace
from uuid import uuid4

import pytest
from merlin_browser.models import BrowserOwner
from merlin_browser.tools import (
    CompiledApiReconHandoffTool,
    _observed_request_bindings,
    _replay_step,
)
from merlin_contracts import ToolContext


def exchange(text, nonce, session, *, status=200):
    return {
        "method": "POST",
        "url": "http://localhost:8000/exchange",
        "status": status,
        "request_json": {"text": text, "box": [{"opaque": nonce}], "session": session},
        "response_json": {"reply": "received"},
    }


def test_inference_uses_changed_values_not_target_or_field_names():
    session_id = str(uuid4())
    first = exchange("first", str(uuid4()), session_id)
    second = exchange("second", str(uuid4()), session_id)
    template = {**second["request_json"], "text": "{{input}}"}
    assert _observed_request_bindings(
        second["request_json"], template, (first, second), "POST", first["url"], "/text"
    ) == {"/box/0/opaque": "uuid4"}


@pytest.mark.parametrize("case", ["single", "same_prompt", "rejected", "server_issued", "capture"])
def test_inference_does_not_guess_unknown_or_server_sourced_tokens(case):
    first = exchange("first", str(uuid4()), "session")
    second = exchange("second", str(uuid4()), "session")
    events = [first, second]
    template = {**second["request_json"], "text": "{{input}}"}
    if case == "single":
        events = [second]
    elif case == "same_prompt":
        first["request_json"]["text"] = "second"
    elif case == "rejected":
        first["status"] = 409
    elif case == "server_issued":
        first["response_json"]["nextToken"] = second["request_json"]["box"][0]["opaque"]
    elif case == "capture":
        template["box"] = [{"opaque": "{{capture:server_nonce}}"}]
    assert not _observed_request_bindings(
        second["request_json"], template, tuple(events), "POST", first["url"], "/text"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["declared", "observed", "unknown"])
async def test_recon_compiles_declared_or_observed_nested_request_binding(mode):
    captured = exchange("harmless probe", str(uuid4()), "reviewed-session")
    previous = exchange("first harmless probe", str(uuid4()), "reviewed-session")

    class Session:
        context_id = "browser-context"
        bounds = SimpleNamespace(target_id="target")
        owner = BrowserOwner(session_id="s1", worker_id="a1", technique_id="recon")

        def api_exchange_events(self):
            return (previous, captured) if mode == "observed" else (captured,)

    class Recorder:
        handoff = None

        async def record_handoff(self, value):
            self.handoff = value

        async def reviewed_native_input_receipt(self, context_id, event, input_pointer):
            return {
                "context_id": context_id,
                "target_id": "target",
                "input_pointer": input_pointer,
                "evidence_ref": "native-request:sha256:" + "a" * 64,
            }

    recorder = Recorder()
    result = await CompiledApiReconHandoffTool(lambda _: Session(), recorder).invoke(
        ToolContext(session_id="s1", workspace_root="/tmp"),
        {
            "method": "POST",
            "url": captured["url"],
            "input_pointer": "/text",
            "response_selector": "/reply",
            "authentication": "none",
            "session_constraint": "unverified",
            **({"request_bindings": {"/box/0/opaque": "uuid4"}} if mode == "declared" else {}),
        },
    )
    assert not result.is_error, result.content
    adapter = json.loads(recorder.handoff.adapter)
    step = next(step for step in adapter["api_steps"] if step["interaction"])
    if mode == "unknown":
        assert not step.get("request_bindings")
        assert any(
            "no established per-request generation contract" in item["reason"]
            for item in adapter["evaluation_uncertainties"]
        )
    else:
        assert step["request_bindings"] == {"/box/0/opaque": "uuid4"}
    assert step["request_template"]["session"] == "reviewed-session"
    assert step["request_template"]["text"] == "{{input}}"


@pytest.mark.asyncio
async def test_preparation_replay_uses_generated_values_too():
    requests = []

    async def requester(method, url, body):
        requests.append(body)
        return 200, {"reply": "received"}

    step = {
        "step_id": "message",
        "method": "POST",
        "endpoint": "http://localhost:8000/exchange",
        "request_template": {"text": "{{input}}", "opaque": "captured-value"},
        "request_bindings": {"/opaque": "uuid4"},
        "expected_status": 200,
        "response_selector": "/reply",
    }
    assert (await _replay_step(requester, step, {}))[1] is None
    assert (await _replay_step(requester, step, {}))[1] is None
    assert requests[0]["opaque"] != requests[1]["opaque"]
