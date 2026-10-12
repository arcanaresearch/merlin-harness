"""Generic stateful HTTP target used by campaign integration tests.

The fixture deliberately separates logical leases from physical target state.
That lets tests reproduce the failure mode where N campaign candidates appear
to own independent sessions while the remote target aliases them onto M<N
mutable conversations.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from hashlib import sha256
from typing import Any, Literal


def _digest(value: object) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return "sha256:" + sha256(encoded).hexdigest()


def _state_selector(step_id: str, pointer: str) -> dict[str, str]:
    return {
        "step_id": step_id,
        "evaluation_root": "response_body",
        "pointer": pointer,
    }


@dataclass
class StatefulTargetFixtureServer:
    """Small target with observable state, bounded input, and session aliasing."""

    maximum_parallel_states: int = 2
    input_limit: int = 48
    include_observations: bool = True
    advertise_input_limit: bool = True
    state_mode: Literal["aliased", "fresh", "checkpoint"] = "aliased"
    port: int = 0
    http_log: list[dict[str, Any]] = field(default_factory=list)
    _server: asyncio.AbstractServer | None = field(default=None, init=False, repr=False)
    _client_tasks: set[asyncio.Task[None]] = field(default_factory=set, init=False, repr=False)
    _client_writers: set[asyncio.StreamWriter] = field(default_factory=set, init=False, repr=False)
    _leases: dict[str, str] = field(default_factory=dict, init=False, repr=False)
    _states: dict[str, dict[str, object]] = field(default_factory=dict, init=False, repr=False)
    _lease_sequence: int = field(default=0, init=False, repr=False)
    _closing: bool = field(default=False, init=False, repr=False)

    def __post_init__(self) -> None:
        if self.maximum_parallel_states < 1:
            raise ValueError("maximum_parallel_states must be positive")
        if self.input_limit < 1:
            raise ValueError("input_limit must be positive")

    @property
    def origin(self) -> str:
        assert self._server is not None and self._server.sockets
        port = int(self._server.sockets[0].getsockname()[1])
        return f"http://127.0.0.1:{port}"

    async def __aenter__(self) -> StatefulTargetFixtureServer:
        self._closing = False
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", self.port)
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.close()

    async def close(self) -> None:
        self._closing = True
        server, self._server = self._server, None
        if server is not None:
            server.close()
        writers = tuple(self._client_writers)
        for writer in writers:
            writer.close()
        current = asyncio.current_task()
        tasks = tuple(task for task in self._client_tasks if task is not current)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        if writers:
            await asyncio.gather(
                *(writer.wait_closed() for writer in writers), return_exceptions=True
            )
        if server is not None:
            await server.wait_closed()
        self._client_tasks.clear()
        self._client_writers.clear()

    def adapter(self) -> dict[str, object]:
        """Return reviewed adapter data without target-specific runtime code."""
        baseline_step = "restore" if self.state_mode == "checkpoint" else "start"
        affordance = {
            "aliased": "provision_state",
            "fresh": "provision_fresh_state",
            "checkpoint": "restore_checkpoint",
        }[self.state_mode]
        adapter: dict[str, object] = {
            "interface": "http_api",
            "endpoint": self.origin + "/api/",
            "execution_mode": "api",
            "available_execution_modes": ["api"],
            "request_constraints": [
                {
                    "path": "/message",
                    "kind": "string_length",
                    "step_id": "interaction",
                    "evaluation_root": "request_body",
                    "maximum": self.input_limit,
                    "unit": "unicode_characters",
                    "source": "server_validation_response",
                    "evidence_refs": ["evidence:fixture-input-limit"],
                    "evidence_bindings": [
                        {
                            "evidence_ref": "evidence:fixture-input-limit",
                            "observed_maximum": self.input_limit,
                        }
                    ],
                    "confidence": 1.0,
                }
            ],
            "state_capabilities": {
                "identity": {
                    "baseline_selector": _state_selector(baseline_step, "/state/id"),
                    "result_selector": _state_selector("interaction", "/state/id"),
                },
                "affordances": [
                    {
                        "kind": affordance,
                        "step_id": baseline_step,
                        "evaluation_root": "response_body",
                        "evidence_refs": [f"evidence:fixture-{baseline_step}"],
                    },
                ],
                "maximum_parallel_states": self.maximum_parallel_states,
            },
            "api_steps": [
                {
                    "step_id": "start",
                    "endpoint": "start",
                    "method": "POST",
                    "request_template": {},
                    "response_selector": "/leaseId",
                    "expected_status": 200,
                    "success_witness": "lease",
                    "cleanup": False,
                    "interaction": False,
                    "captures": {"lease_id": "/leaseId"},
                    "repeat": 1,
                    "idempotency_key": None,
                    "replay_after_ambiguous_result": False,
                },
                {
                    "step_id": "interaction",
                    "endpoint": "message",
                    "method": "POST",
                    "request_template": {
                        "leaseId": "{{capture:lease_id}}",
                        "message": "{{input}}",
                    },
                    "response_selector": "/reply",
                    "expected_status": 200,
                    "success_witness": "reply",
                    "cleanup": False,
                    "interaction": True,
                    "captures": {},
                    "repeat": 1,
                    "idempotency_key": None,
                    "replay_after_ambiguous_result": False,
                    "structured_constraint_rejections": [
                        {
                            "kind": "string_length",
                            "status_codes": [422],
                            "request_path": "/message",
                            "maximum_pointer": "/error/maximum",
                            "unit": "unicode_characters",
                        }
                    ],
                },
                {
                    "step_id": "cleanup",
                    "endpoint": "cleanup",
                    "method": "POST",
                    "request_template": {"leaseId": "{{capture:lease_id}}"},
                    "response_selector": "/closed",
                    "expected_status": 200,
                    "success_witness": "closed",
                    "cleanup": True,
                    "interaction": False,
                    "captures": {},
                    "cleanup_acknowledgement": {
                        "pointer": "/closed",
                        "match_values": [True],
                        "status_codes": [200],
                    },
                    "repeat": 1,
                    "idempotency_key": None,
                    "replay_after_ambiguous_result": False,
                },
            ],
        }
        if not self.advertise_input_limit:
            adapter.pop("request_constraints")
        capabilities = adapter["state_capabilities"]
        assert isinstance(capabilities, dict)
        if self.state_mode == "fresh":
            capabilities["freshness_rules"] = [
                {
                    "selector": _state_selector("start", "/state/fresh"),
                    "match_values": [True],
                    "evidence_refs": ["evidence:fixture-start"],
                }
            ]
        if self.state_mode == "checkpoint":
            steps = adapter["api_steps"]
            assert isinstance(steps, list)
            steps.insert(
                1,
                {
                    "step_id": "restore",
                    "endpoint": "restore",
                    "method": "POST",
                    "request_template": {"leaseId": "{{capture:lease_id}}"},
                    "response_selector": "/restored",
                    "expected_status": 200,
                    "success_witness": "restored",
                    "cleanup": False,
                    "interaction": False,
                    "captures": {},
                    "repeat": 1,
                    "idempotency_key": None,
                    "replay_after_ambiguous_result": False,
                },
            )
        if self.include_observations:
            capabilities["observations"] = [
                {
                    "name": "physical_state_id",
                    "role": "state_identity",
                    "baseline_selector": _state_selector(baseline_step, "/state/id"),
                    "result_selector": _state_selector("interaction", "/state/id"),
                },
                {
                    "name": "progress_stage",
                    "role": "progress_ordinal",
                    "baseline_selector": _state_selector(baseline_step, "/state/stage"),
                    "result_selector": _state_selector("interaction", "/state/stage"),
                    "preferred_direction": "increase",
                },
                {
                    "name": "objective_score",
                    "role": "score",
                    "baseline_selector": _state_selector(baseline_step, "/state/score"),
                    "result_selector": _state_selector("interaction", "/state/score"),
                    "preferred_direction": "increase",
                },
                {
                    "name": "remaining_budget",
                    "role": "budget_remaining",
                    "baseline_selector": _state_selector(baseline_step, "/state/turnsRemaining"),
                    "result_selector": _state_selector("interaction", "/state/turnsRemaining"),
                    "preferred_direction": "increase",
                },
                {
                    "name": "terminal_state",
                    "role": "terminal_status",
                    "baseline_selector": _state_selector(baseline_step, "/state/status"),
                    "result_selector": _state_selector("interaction", "/state/status"),
                    "success_values": ["won"],
                    "failure_values": ["lost"],
                },
            ]
        return adapter

    def adapter_digest(self) -> str:
        return _digest({**self.adapter(), "endpoint": "<origin>/api/"})

    def physical_state_for(self, lease_id: str) -> str | None:
        return self._leases.get(lease_id)

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        if self._closing:
            writer.close()
            await writer.wait_closed()
            return
        task = asyncio.current_task()
        if task is not None:
            self._client_tasks.add(task)
            task.add_done_callback(self._client_tasks.discard)
        self._client_writers.add(writer)
        try:
            method, path, body = await self._read_request(reader)
            operation = path.rstrip("/").rsplit("/", 1)[-1]
            request = json.loads(body) if body else {}
            status, payload = self._response(method, operation, request)
            self.http_log.append(
                {
                    "method": method,
                    "path": path,
                    "operation": operation,
                    "status": status,
                    "request": request,
                    "response": payload,
                }
            )
            encoded = json.dumps(payload, separators=(",", ":")).encode()
            reason = "OK" if status == 200 else "Unprocessable Entity"
            writer.write(
                f"HTTP/1.1 {status} {reason}\r\n".encode()
                + b"Content-Type: application/json\r\nContent-Length: "
                + str(len(encoded)).encode()
                + b"\r\nConnection: close\r\n\r\n"
                + encoded
            )
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()
            self._client_writers.discard(writer)

    async def _read_request(self, reader: asyncio.StreamReader) -> tuple[str, str, bytes]:
        request_line = await reader.readline()
        method, path, _version = request_line.decode().strip().split(" ", 2)
        headers: dict[str, str] = {}
        while True:
            line = await reader.readline()
            if line in {b"\r\n", b""}:
                break
            name, value = line.decode().split(":", 1)
            headers[name.lower()] = value.strip()
        body = await reader.readexactly(int(headers.get("content-length", "0") or "0"))
        return method, path, body

    def _response(
        self, method: str, operation: str, request: dict[str, object]
    ) -> tuple[int, dict[str, object]]:
        if method == "POST" and operation == "start":
            self._lease_sequence += 1
            lease_id = f"logical-lease-{self._lease_sequence}"
            slot = (
                self._lease_sequence
                if self.state_mode == "fresh"
                else ((self._lease_sequence - 1) % self.maximum_parallel_states) + 1
            )
            state_id = f"physical-state-{slot}"
            self._leases[lease_id] = state_id
            was_created = state_id not in self._states
            self._states.setdefault(
                state_id,
                {
                    "id": state_id,
                    "fresh": True,
                    "stage": 1,
                    "score": 0,
                    "turnsRemaining": 4,
                    "status": "active",
                },
            )
            state = dict(self._states[state_id])
            state["fresh"] = was_created
            return 200, {"leaseId": lease_id, "state": state}

        lease_id = str(request.get("leaseId", ""))
        state_id = self._leases.get(lease_id)
        if state_id is None:
            return 422, {"error": {"code": "unknown_lease"}}

        if method == "POST" and operation == "message":
            message = str(request.get("message", ""))
            if len(message) > self.input_limit:
                return 422, {
                    "error": {
                        "code": "input_too_long",
                        "path": "/message",
                        "maximum": self.input_limit,
                        "unit": "unicode_characters",
                    },
                    "state": dict(self._states[state_id]),
                }
            state = self._states[state_id]
            state["score"] = int(state["score"]) + 10
            state["turnsRemaining"] = int(state["turnsRemaining"]) - 1
            state["stage"] = 1 + int(state["score"]) // 20
            if int(state["score"]) >= 40:
                state["status"] = "won"
            elif int(state["turnsRemaining"]) <= 0:
                state["status"] = "lost"
            return 200, {"reply": f"accepted: {message}", "state": dict(state)}

        if method == "POST" and operation == "restore":
            state = self._states[state_id]
            state.update(
                {
                    "fresh": False,
                    "stage": 1,
                    "score": 0,
                    "turnsRemaining": 4,
                    "status": "active",
                }
            )
            return 200, {"restored": True, "state": dict(state)}

        if method == "POST" and operation == "cleanup":
            self._leases.pop(lease_id, None)
            return 200, {"closed": True, "state": dict(self._states[state_id])}

        return 422, {"error": {"code": "unsupported_operation"}}
