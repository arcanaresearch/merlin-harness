from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path

import httpx
import pytest
from merlin_contracts import PromotionCandidate, promotion_digest
from merlin_promotion import (
    AdapterCaseEvaluator,
    CandidateCaseEvaluator,
    EvaluationBinding,
    EvaluationCase,
    ProviderCaseEvaluator,
    load_adapter_evaluation_fixture,
)


def _write_fixture(path: Path, endpoint: str) -> EvaluationCase:
    value = {
        "schema_version": "merlin.adapter-evaluation.v2",
        "adapter_id": "refund-api-v1",
        "interface": "http_api",
        "endpoint": endpoint,
        "credential_handle": "target-token",
        "steps": [
            {
                "id": "start",
                "method": "POST",
                "path": "/start",
                "request": {"game": "refund"},
                "response_pointer": "/session/id",
                "expected_value": {"$merlin": "non_empty_string"},
                "expected_status": 200,
                "cleanup": False,
                "idempotency_key": "evaluation-start-1",
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
                "idempotency_key": "evaluation-message-1",
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
                "idempotency_key": "evaluation-cleanup-1",
                "replay_after_ambiguous_result": True,
                "capture_as": None,
            },
        ],
    }
    payload = json.dumps(value, sort_keys=True).encode()
    path.write_bytes(payload)
    path.chmod(0o600)
    return EvaluationCase(
        id="adapter-lifecycle",
        target_fixture=path.as_uri(),
        source_group="heldout-target",
        fixture_digest=f"sha256:{hashlib.sha256(payload).hexdigest()}",
    )


def _candidate(endpoint: str) -> PromotionCandidate:
    content: dict[str, object] = {
        "adapter_id": "refund-api-v1",
        "interface": "http_api",
        "endpoint": endpoint,
        "credential_handle": "target-token",
    }
    return PromotionCandidate(
        id="candidate-adapter",
        kind="adapter",
        version="1",
        baseline_version="0",
        baseline_digest="sha256:baseline",
        content=content,
        content_digest=promotion_digest(content),
        proposed_by="operator:test",
    )


def _binding() -> EvaluationBinding:
    return EvaluationBinding(
        suite_id="adapter-suite",
        provider="target",
        model="n/a",
        route="refund-api-v1",
        budget={"case_timeout_seconds": 5, "max_steps": 4},
        dataset_digest="sha256:dataset",
    )


def _disable_message_replay(case: EvaluationCase) -> EvaluationCase:
    fixture_path = Path(case.target_fixture.removeprefix("file://"))
    value = json.loads(fixture_path.read_text())
    value["steps"][1]["replay_after_ambiguous_result"] = False
    payload = json.dumps(value, sort_keys=True).encode()
    fixture_path.write_bytes(payload)
    return EvaluationCase(
        id=case.id,
        target_fixture=case.target_fixture,
        source_group=case.source_group,
        fixture_digest="sha256:" + hashlib.sha256(payload).hexdigest(),
    )


def _select_whole_message_response(case: EvaluationCase) -> EvaluationCase:
    fixture_path = Path(case.target_fixture.removeprefix("file://"))
    value = json.loads(fixture_path.read_text())
    value["steps"][1]["response_pointer"] = ""
    value["steps"][1]["expected_value"] = {"reply": "observed"}
    payload = json.dumps(value, sort_keys=True).encode()
    fixture_path.write_bytes(payload)
    return EvaluationCase(
        id=case.id,
        target_fixture=case.target_fixture,
        source_group=case.source_group,
        fixture_digest="sha256:" + hashlib.sha256(payload).hexdigest(),
    )


def test_adapter_fixture_requires_final_cleanup_and_digest(tmp_path: Path) -> None:
    case = _write_fixture(tmp_path / "adapter.json", "https://target.test")
    fixture = load_adapter_evaluation_fixture(case)
    assert fixture.steps[-1].cleanup is True
    with pytest.raises(ValueError, match="digest mismatch"):
        load_adapter_evaluation_fixture(
            EvaluationCase(
                id=case.id,
                target_fixture=case.target_fixture,
                source_group=case.source_group,
                fixture_digest="sha256:" + "0" * 64,
            )
        )


def test_adapter_fixture_accepts_json_pointer_root_and_rejects_bare_member_name(
    tmp_path: Path,
) -> None:
    case = _write_fixture(tmp_path / "adapter.json", "https://target.test")
    root_case = _select_whole_message_response(case)

    fixture = load_adapter_evaluation_fixture(root_case)
    assert fixture.steps[1].response_pointer == ""

    fixture_path = Path(case.target_fixture.removeprefix("file://"))
    value = json.loads(fixture_path.read_text())
    value["steps"][1]["response_pointer"] = "reply"
    payload = json.dumps(value, sort_keys=True).encode()
    fixture_path.write_bytes(payload)
    invalid_case = EvaluationCase(
        id=case.id,
        target_fixture=case.target_fixture,
        source_group=case.source_group,
        fixture_digest="sha256:" + hashlib.sha256(payload).hexdigest(),
    )
    with pytest.raises(ValueError, match="empty for the whole response"):
        load_adapter_evaluation_fixture(invalid_case)


@pytest.mark.asyncio
async def test_adapter_evaluator_can_match_the_whole_json_response(tmp_path: Path) -> None:
    endpoint = "https://target.test"
    case = _select_whole_message_response(_write_fixture(tmp_path / "adapter.json", endpoint))

    def handler(request: httpx.Request) -> httpx.Response:
        bodies = {
            "/start": {"session": {"id": "session-root-42"}},
            "/message": {"reply": "observed"},
            "/abandon": {"closed": True},
        }
        return httpx.Response(200, json=bodies[request.url.path])

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        metrics = await AdapterCaseEvaluator({"target-token": "opaque-secret"}, client=client)(
            _candidate(endpoint), case, _binding()
        )

    assert metrics.task_success_rate == 1
    assert metrics.evidence_attribution_rate == 1


@pytest.mark.asyncio
async def test_adapter_evaluator_executes_auth_idempotency_and_cleanup(tmp_path: Path) -> None:
    endpoint = "https://target.test"
    case = _write_fixture(tmp_path / "adapter.json", endpoint)
    calls: list[tuple[str, str]] = []
    ambiguous_start = True

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal ambiguous_start
        assert request.headers["authorization"] == "Bearer opaque-secret"
        key = request.headers["idempotency-key"]
        calls.append((request.url.path, key))
        if request.url.path == "/start" and ambiguous_start:
            ambiguous_start = False
            raise httpx.ReadError("response lost after target accepted request", request=request)
        if request.url.path != "/start":
            assert json.loads(request.content)["session"] == "session-random-42"
        bodies = {
            "/start": {"session": {"id": "session-random-42"}},
            "/message": {"reply": "observed"},
            "/abandon": {"closed": True},
        }
        return httpx.Response(200, json=bodies[request.url.path])

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        metrics = await AdapterCaseEvaluator({"target-token": "opaque-secret"}, client=client)(
            _candidate(endpoint), case, _binding()
        )
    assert metrics.task_success_rate == 1
    assert metrics.evidence_attribution_rate == 1
    assert metrics.cancellation_recovery_rate == 1
    assert calls.count(("/start", "evaluation-start-1")) == 2
    assert calls.count(("/abandon", "evaluation-cleanup-1")) == 2


@pytest.mark.asyncio
async def test_adapter_evaluator_does_not_replay_undeclared_ambiguous_effect(
    tmp_path: Path,
) -> None:
    endpoint = "https://target.test"
    case = _disable_message_replay(_write_fixture(tmp_path / "adapter.json", endpoint))
    message_calls = 0
    cleanup_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal cleanup_calls, message_calls
        if request.url.path == "/start":
            return httpx.Response(200, json={"session": {"id": "session-1"}})
        if request.url.path == "/message":
            message_calls += 1
            raise httpx.ReadTimeout("ambiguous message result", request=request)
        cleanup_calls += 1
        return httpx.Response(200, json={"closed": True})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ValueError, match="message has an ambiguous result"):
            await AdapterCaseEvaluator({"target-token": "opaque-secret"}, client=client)(
                _candidate(endpoint), case, _binding()
            )
    assert message_calls == 1
    assert cleanup_calls == 2


@pytest.mark.asyncio
async def test_adapter_evaluator_runs_cleanup_when_cancelled(tmp_path: Path) -> None:
    endpoint = "https://target.test"
    case = _write_fixture(tmp_path / "adapter.json", endpoint)
    message_started = asyncio.Event()
    cleanup_calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal cleanup_calls
        if request.url.path == "/start":
            return httpx.Response(200, json={"session": {"id": "session-1"}})
        if request.url.path == "/message":
            message_started.set()
            await asyncio.Future()
        cleanup_calls += 1
        return httpx.Response(200, json={"closed": True})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        task = asyncio.create_task(
            AdapterCaseEvaluator({"target-token": "opaque-secret"}, client=client)(
                _candidate(endpoint), case, _binding()
            )
        )
        await message_started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert cleanup_calls == 2


@pytest.mark.asyncio
async def test_adapter_evaluator_fails_closed_on_cleanup_mismatch(tmp_path: Path) -> None:
    endpoint = "https://target.test"
    case = _write_fixture(tmp_path / "adapter.json", endpoint)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/start":
            return httpx.Response(200, json={"session": {"id": "session-1"}})
        if request.url.path == "/message":
            return httpx.Response(200, json={"reply": "observed"})
        return httpx.Response(200, json={"closed": False})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        metrics = await AdapterCaseEvaluator({"target-token": "opaque-secret"}, client=client)(
            _candidate(endpoint), case, _binding()
        )
    assert metrics.task_success_rate == 0
    assert metrics.cancellation_recovery_rate == 0


@pytest.mark.asyncio
async def test_candidate_dispatcher_rejects_unsupported_domains() -> None:
    evaluator = CandidateCaseEvaluator(
        ProviderCaseEvaluator(object()),  # type: ignore[arg-type]
        AdapterCaseEvaluator({}),
    )
    content = {"instructions": "x"}
    candidate = PromotionCandidate(
        id="guidance",
        kind="guidance",
        version="1",
        baseline_version="0",
        baseline_digest="sha256:baseline",
        content=content,
        content_digest=promotion_digest(content),
        proposed_by="operator:test",
    )
    with pytest.raises(ValueError, match="no production evaluator"):
        await evaluator(
            candidate,
            EvaluationCase(
                id="unused",
                target_fixture="file:///unused",
                source_group="unused",
                fixture_digest="sha256:" + "0" * 64,
            ),
            _binding(),
        )
