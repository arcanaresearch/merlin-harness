from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from time import perf_counter
from typing import Any
from urllib.parse import unquote, urljoin, urlsplit

import httpx
from merlin_contracts import EvaluationMetrics, PromotionCandidate

from .provider_evaluator import _budget_int, _read_owned_file
from .service import EvaluationBinding, EvaluationCase


@dataclass(frozen=True, slots=True)
class AdapterEvaluationStep:
    id: str
    method: str
    path: str
    request: object
    response_pointer: str
    expected_value: object
    expected_status: int
    cleanup: bool
    idempotency_key: str | None
    replay_after_ambiguous_result: bool
    capture_as: str | None


@dataclass(frozen=True, slots=True)
class AdapterEvaluationFixture:
    adapter_id: str
    interface: str
    endpoint: str
    credential_handle: str | None
    steps: tuple[AdapterEvaluationStep, ...]


def load_adapter_evaluation_fixture(
    case: EvaluationCase, *, max_bytes: int = 1_048_576
) -> AdapterEvaluationFixture:
    parsed = urlsplit(case.target_fixture)
    if parsed.scheme != "file" or parsed.netloc not in {"", "localhost"}:
        raise ValueError("adapter evaluation fixture must use an absolute local file URI")
    path = Path(unquote(parsed.path))
    if not path.is_absolute():
        raise ValueError("adapter evaluation fixture path must be absolute")
    payload = _read_owned_file(path, max_bytes=max_bytes)
    if f"sha256:{sha256(payload).hexdigest()}" != case.fixture_digest:
        raise ValueError("adapter evaluation fixture digest mismatch")
    try:
        value = json.loads(payload.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as error:
        raise ValueError("adapter evaluation fixture is not valid UTF-8 JSON") from error
    expected = {
        "schema_version",
        "adapter_id",
        "interface",
        "endpoint",
        "credential_handle",
        "steps",
    }
    if not isinstance(value, dict) or set(value) != expected:
        raise ValueError("adapter evaluation fixture has an unexpected schema")
    if value["schema_version"] != "merlin.adapter-evaluation.v2":
        raise ValueError("adapter evaluation fixture has an unsupported schema version")
    for field in ("adapter_id", "interface", "endpoint"):
        if not isinstance(value[field], str) or not value[field].strip():
            raise TypeError(f"adapter evaluation {field} must be a non-empty string")
    if value["interface"] not in {"http_api", "open_ai_compatible"}:
        raise ValueError("adapter evaluation currently supports HTTP API interfaces only")
    endpoint = urlsplit(value["endpoint"])
    if endpoint.scheme not in {"http", "https"} or not endpoint.netloc:
        raise ValueError("adapter evaluation endpoint must be an absolute HTTP(S) URL")
    if endpoint.username is not None or endpoint.password is not None:
        raise ValueError("adapter evaluation endpoint must not contain credentials")
    credential_handle = value["credential_handle"]
    if credential_handle is not None and (
        not isinstance(credential_handle, str) or not credential_handle.strip()
    ):
        raise TypeError("adapter evaluation credential_handle must be null or non-empty")
    raw_steps = value["steps"]
    if not isinstance(raw_steps, list) or not raw_steps:
        raise TypeError("adapter evaluation steps must be a non-empty array")
    steps = tuple(_step(item) for item in raw_steps)
    if len({step.id for step in steps}) != len(steps):
        raise ValueError("adapter evaluation step ids must be unique")
    captures = [step.capture_as for step in steps if step.capture_as is not None]
    if len(set(captures)) != len(captures):
        raise ValueError("adapter evaluation capture names must be unique")
    if steps[-1].cleanup is not True or any(step.cleanup for step in steps[:-1]):
        raise ValueError("adapter evaluation requires one final cleanup step")
    if not any(not step.cleanup for step in steps):
        raise ValueError("adapter evaluation requires an interaction step")
    return AdapterEvaluationFixture(
        adapter_id=value["adapter_id"],
        interface=value["interface"],
        endpoint=value["endpoint"].rstrip("/") + "/",
        credential_handle=credential_handle,
        steps=steps,
    )


class AdapterCaseEvaluator:
    """Execute one immutable reviewed HTTP adapter lifecycle against its exact target."""

    def __init__(
        self,
        credentials: Mapping[str, str],
        *,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._credentials = credentials
        self._client = client

    async def __call__(
        self,
        candidate: PromotionCandidate,
        case: EvaluationCase,
        binding: EvaluationBinding,
    ) -> EvaluationMetrics:
        if candidate.kind != "adapter":
            raise ValueError("adapter evaluation suites accept adapter candidates only")
        fixture = load_adapter_evaluation_fixture(case)
        exact = {
            "adapter_id": fixture.adapter_id,
            "interface": fixture.interface,
            "endpoint": fixture.endpoint.rstrip("/"),
            "credential_handle": fixture.credential_handle,
        }
        if any(candidate.content.get(key) != value for key, value in exact.items()):
            raise ValueError("adapter candidate does not match the evaluation fixture")
        if binding.route != fixture.adapter_id:
            raise ValueError("adapter evaluation route must name the exact adapter id")
        timeout = _budget_int(binding, "case_timeout_seconds", default=120, minimum=1, maximum=900)
        max_steps = _budget_int(binding, "max_steps", default=32, minimum=2, maximum=1_000)
        if len(fixture.steps) > max_steps:
            raise ValueError("adapter evaluation fixture exceeds the suite step cap")
        headers: dict[str, str] = {}
        if fixture.credential_handle is not None:
            secret = self._credentials.get(fixture.credential_handle)
            if not secret:
                raise ValueError("adapter evaluation credential handle is unavailable")
            headers["Authorization"] = f"Bearer {secret}"
        started = perf_counter()
        succeeded = True
        attributed = 0
        cleanup_succeeded = False
        variables: dict[str, object] = {}
        owns_client = self._client is None
        client = self._client or httpx.AsyncClient(timeout=timeout)
        try:
            async with asyncio.timeout(timeout):
                pending_error: BaseException | None = None
                for step in fixture.steps[:-1]:
                    try:
                        step_ok, observed = await _execute_step(
                            client, fixture, step, headers, variables
                        )
                    except (Exception, asyncio.CancelledError) as error:
                        pending_error = error
                        succeeded = False
                        break
                    succeeded = succeeded and step_ok
                    attributed += int(step_ok)
                    if step_ok and step.capture_as is not None:
                        variables[step.capture_as] = observed
                cleanup = fixture.steps[-1]
                try:
                    cleanup_succeeded, _ = await _execute_step(
                        client, fixture, cleanup, headers, variables
                    )
                    attributed += int(cleanup_succeeded)
                    succeeded = succeeded and cleanup_succeeded
                except (Exception, asyncio.CancelledError) as error:
                    succeeded = False
                    if pending_error is None:
                        pending_error = error
                if pending_error is not None:
                    raise pending_error
        finally:
            if owns_client:
                await client.aclose()
        latency_ms = max(0, int((perf_counter() - started) * 1_000))
        return EvaluationMetrics(
            task_success_rate=float(succeeded and cleanup_succeeded),
            false_positive_rate=float(not succeeded),
            evidence_attribution_rate=attributed / len(fixture.steps),
            authority_violations=0,
            mean_cost=0,
            p95_latency_ms=latency_ms,
            cancellation_recovery_rate=float(cleanup_succeeded),
        )


async def _request(
    client: httpx.AsyncClient,
    fixture: AdapterEvaluationFixture,
    step: AdapterEvaluationStep,
    headers: Mapping[str, str],
    variables: Mapping[str, object],
) -> httpx.Response:
    return await client.request(
        step.method,
        urljoin(fixture.endpoint, step.path.lstrip("/")),
        json=_render_template(step.request, variables),
        headers=headers,
    )


async def _execute_step(
    client: httpx.AsyncClient,
    fixture: AdapterEvaluationFixture,
    step: AdapterEvaluationStep,
    headers: Mapping[str, str],
    variables: Mapping[str, object],
) -> tuple[bool, object]:
    step_headers = dict(headers)
    if step.idempotency_key is not None:
        step_headers["Idempotency-Key"] = step.idempotency_key
    response: httpx.Response | None = None
    try:
        response = await _request(client, fixture, step, step_headers, variables)
    except (httpx.ReadError, httpx.ReadTimeout, httpx.RemoteProtocolError):
        if not step.replay_after_ambiguous_result:
            raise ValueError(f"adapter evaluation step {step.id} has an ambiguous result") from None
    if step.replay_after_ambiguous_result:
        replay = await _request(client, fixture, step, step_headers, variables)
        if response is not None and (
            replay.status_code != response.status_code
            or _observe(replay, step) != _observe(response, step)
        ):
            raise ValueError(f"adapter evaluation step {step.id} replay changed its result")
        response = replay
    assert response is not None
    observed = _observe(response, step)
    return (
        response.status_code == step.expected_status
        and _matches_expected(observed, step.expected_value),
        observed,
    )


def _step(value: object) -> AdapterEvaluationStep:
    expected = {
        "id",
        "method",
        "path",
        "request",
        "response_pointer",
        "expected_value",
        "expected_status",
        "cleanup",
        "idempotency_key",
        "replay_after_ambiguous_result",
        "capture_as",
    }
    if not isinstance(value, dict) or set(value) != expected:
        raise ValueError("adapter evaluation step has an unexpected schema")
    for field in ("id", "method", "path"):
        if not isinstance(value[field], str) or not value[field].strip():
            raise TypeError(f"adapter evaluation step {field} must be non-empty")
    response_pointer = value["response_pointer"]
    if not isinstance(response_pointer, str):
        raise TypeError("adapter evaluation step response_pointer must be a string")
    if response_pointer and not response_pointer.startswith("/"):
        raise ValueError(
            "adapter evaluation response_pointer must be empty for the whole response "
            "or start with '/'"
        )
    method = value["method"].upper()
    if method not in {"GET", "POST", "PUT", "PATCH", "DELETE"}:
        raise ValueError("adapter evaluation step method is unsupported")
    status = value["expected_status"]
    if isinstance(status, bool) or not isinstance(status, int) or not 100 <= status <= 599:
        raise ValueError("adapter evaluation expected_status is invalid")
    if not isinstance(value["cleanup"], bool) or not isinstance(
        value["replay_after_ambiguous_result"], bool
    ):
        raise TypeError("adapter evaluation cleanup/replay values must be booleans")
    idempotency_key = value["idempotency_key"]
    if idempotency_key is not None and (
        not isinstance(idempotency_key, str) or not idempotency_key.strip()
    ):
        raise TypeError("adapter evaluation idempotency_key must be null or non-empty")
    if value["replay_after_ambiguous_result"] and idempotency_key is None:
        raise ValueError("ambiguous-result replay requires an idempotency key")
    capture_as = value["capture_as"]
    if capture_as is not None and (
        not isinstance(capture_as, str)
        or not capture_as.isidentifier()
        or capture_as.startswith("_")
    ):
        raise ValueError("adapter evaluation capture_as must be null or a public identifier")
    oracle = value["expected_value"]
    if (
        isinstance(oracle, dict)
        and set(oracle) == {"$merlin"}
        and oracle["$merlin"]
        not in {
            "non_empty_string",
            "string",
        }
    ):
        raise ValueError("adapter evaluation expected_value oracle is unsupported")
    return AdapterEvaluationStep(
        id=value["id"],
        method=method,
        path=value["path"],
        request=value["request"],
        response_pointer=response_pointer,
        expected_value=value["expected_value"],
        expected_status=status,
        cleanup=value["cleanup"],
        idempotency_key=idempotency_key,
        replay_after_ambiguous_result=value["replay_after_ambiguous_result"],
        capture_as=capture_as,
    )


def _matches_expected(observed: object, expected: object) -> bool:
    if expected == {"$merlin": "non_empty_string"}:
        return isinstance(observed, str) and bool(observed.strip())
    if expected == {"$merlin": "string"}:
        return isinstance(observed, str)
    return observed == expected


def _render_template(value: object, variables: Mapping[str, object]) -> object:
    if isinstance(value, list):
        return [_render_template(item, variables) for item in value]
    if isinstance(value, dict):
        return {key: _render_template(item, variables) for key, item in value.items()}
    if not isinstance(value, str) or "{{" not in value:
        return value
    if not value.startswith("{{") or not value.endswith("}}") or value.count("{{") != 1:
        raise ValueError("adapter evaluation placeholders must occupy the complete string value")
    name = value[2:-2]
    if not name.isidentifier() or name not in variables:
        raise ValueError(f"adapter evaluation placeholder is unavailable: {name}")
    return variables[name]


def _observe(response: httpx.Response, step: AdapterEvaluationStep) -> object:
    try:
        value: Any = response.json()
    except ValueError as error:
        raise ValueError(f"adapter evaluation step {step.id} returned invalid JSON") from error
    if step.response_pointer == "":
        return value
    if not step.response_pointer.startswith("/"):
        raise ValueError("adapter evaluation response_pointer must be a JSON pointer")
    for token in step.response_pointer[1:].split("/"):
        token = token.replace("~1", "/").replace("~0", "~")
        if isinstance(value, dict) and token in value:
            value = value[token]
        elif isinstance(value, list) and token.isdigit() and int(token) < len(value):
            value = value[int(token)]
        else:
            raise ValueError(f"adapter evaluation response pointer missing at step {step.id}")
    return value
