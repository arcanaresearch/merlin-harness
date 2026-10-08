from __future__ import annotations

import hashlib
import json
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest
from merlin_contracts import PromotionCandidate, promotion_digest
from merlin_promotion import AdapterCaseEvaluator, EvaluationBinding, EvaluationCase


class _TargetState:
    def __init__(self) -> None:
        self.session_id = "session-from-live-target"
        self.calls: dict[str, int] = {}
        self.results: dict[str, dict[str, object]] = {}


@contextmanager
def _target_server():
    state = _TargetState()

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_POST(self) -> None:  # noqa: N802
            if self.headers.get("Authorization") != "Bearer acceptance-secret":
                self._reply(401, {"error": "unauthorized"})
                return
            key = self.headers.get("Idempotency-Key", "")
            state.calls[key] = state.calls.get(key, 0) + 1
            length = int(self.headers.get("Content-Length", "0"))
            request = json.loads(self.rfile.read(length) or b"{}")
            if key in state.results:
                self._reply(200, state.results[key])
                return
            if self.path == "/start":
                result: dict[str, object] = {"session": {"id": state.session_id}}
            elif self.path == "/message" and request.get("session") == state.session_id:
                result = {"reply": "observed"}
            elif self.path == "/abandon" and request.get("session") == state.session_id:
                result = {"closed": True}
            else:
                self._reply(409, {"error": "invalid session lifecycle"})
                return
            state.results[key] = result
            self._reply(200, result)

        def log_message(self, format: str, *args: Any) -> None:
            del format, args

        def _reply(self, status: int, value: object) -> None:
            payload = json.dumps(value).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.server_address
        yield f"http://{host}:{port}", state
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _fixture(path: Path, endpoint: str) -> EvaluationCase:
    steps = [
        {
            "id": "start",
            "method": "POST",
            "path": "/start",
            "request": {"game": "refund"},
            "response_pointer": "/session/id",
            "expected_value": {"$merlin": "non_empty_string"},
            "expected_status": 200,
            "cleanup": False,
            "idempotency_key": "live-start",
            "replay_after_ambiguous_result": True,
            "capture_as": "session_id",
        },
        {
            "id": "message",
            "method": "POST",
            "path": "/message",
            "request": {"session": "{{session_id}}", "message": "bounded probe"},
            "response_pointer": "/reply",
            "expected_value": "observed",
            "expected_status": 200,
            "cleanup": False,
            "idempotency_key": "live-message",
            "replay_after_ambiguous_result": False,
            "capture_as": None,
        },
        {
            "id": "cleanup",
            "method": "POST",
            "path": "/abandon",
            "request": {"session": "{{session_id}}"},
            "response_pointer": "/closed",
            "expected_value": True,
            "expected_status": 200,
            "cleanup": True,
            "idempotency_key": "live-cleanup",
            "replay_after_ambiguous_result": True,
            "capture_as": None,
        },
    ]
    document = {
        "schema_version": "merlin.adapter-evaluation.v2",
        "adapter_id": "live-local-api-v1",
        "interface": "http_api",
        "endpoint": endpoint,
        "credential_handle": "live-target-token",
        "steps": steps,
    }
    payload = json.dumps(document, sort_keys=True).encode()
    path.write_bytes(payload)
    path.chmod(0o600)
    return EvaluationCase(
        id="live-local-lifecycle",
        target_fixture=path.as_uri(),
        source_group="heldout-local-target",
        fixture_digest="sha256:" + hashlib.sha256(payload).hexdigest(),
    )


@pytest.mark.asyncio
async def test_real_socket_adapter_lifecycle(tmp_path: Path) -> None:
    with _target_server() as (endpoint, state):
        content: dict[str, object] = {
            "adapter_id": "live-local-api-v1",
            "interface": "http_api",
            "endpoint": endpoint,
            "credential_handle": "live-target-token",
        }
        candidate = PromotionCandidate(
            id="live-local-adapter",
            kind="adapter",
            version="1",
            baseline_version="0",
            baseline_digest="sha256:baseline",
            content=content,
            content_digest=promotion_digest(content),
            proposed_by="operator:acceptance",
        )
        metrics = await AdapterCaseEvaluator({"live-target-token": "acceptance-secret"})(
            candidate,
            _fixture(tmp_path / "live-adapter.json", endpoint),
            EvaluationBinding(
                suite_id="live-adapter-suite",
                provider="target",
                model="n/a",
                route="live-local-api-v1",
                budget={"case_timeout_seconds": 10, "max_steps": 3},
                dataset_digest="sha256:" + "1" * 64,
            ),
        )

    assert metrics.task_success_rate == 1
    assert metrics.evidence_attribution_rate == 1
    assert metrics.cancellation_recovery_rate == 1
    assert state.calls == {"live-start": 2, "live-message": 1, "live-cleanup": 2}
