from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path

import pytest
from merlin_browser import BrowserBounds, BrowserObservation, BrowserOwner
from merlin_contracts import ModelJsonError, PromotionCandidate, promotion_digest
from merlin_promotion import (
    BrowserAdapterCaseEvaluator,
    EvaluationBinding,
    EvaluationCase,
    load_browser_evaluation_fixture,
)
from merlin_promotion.browser_evaluator import BrowserEvaluationStep, _observe_result


def _case(path: Path, endpoint: str, *, credential_handle: str | None = None) -> EvaluationCase:
    value = {
        "schema_version": "merlin.browser-adapter-evaluation.v1",
        "adapter_id": "refund-browser-v1",
        "interface": "browser",
        "endpoint": endpoint,
        "credential_handle": credential_handle,
        "steps": [
            {
                "id": "open",
                "tool": "browser_open_tab",
                "arguments": {"url": endpoint},
                "response_pointer": "/tab_ref",
                "expected_value": {"$merlin": "non_empty_string"},
                "capture_as": "tab_ref",
            },
            {
                "id": "observe",
                "tool": "browser_snapshot",
                "arguments": {"tab_ref": "{{tab_ref}}"},
                "response_pointer": "/text",
                "expected_value": "reviewed target ready",
                "capture_as": None,
            },
        ],
    }
    payload = json.dumps(value, sort_keys=True).encode()
    path.write_bytes(payload)
    path.chmod(0o600)
    return EvaluationCase(
        id="browser-lifecycle",
        target_fixture=path.as_uri(),
        source_group="heldout-browser",
        fixture_digest="sha256:" + hashlib.sha256(payload).hexdigest(),
    )


def _candidate(endpoint: str, *, credential_handle: str | None = None) -> PromotionCandidate:
    content: dict[str, object] = {
        "adapter_id": "refund-browser-v1",
        "interface": "browser",
        "endpoint": endpoint,
        "credential_handle": credential_handle,
    }
    return PromotionCandidate(
        id="candidate-browser",
        kind="adapter",
        version="1",
        baseline_version="0",
        baseline_digest="sha256:baseline",
        content=content,
        content_digest=promotion_digest(content),
        proposed_by="operator:test",
    )


def _binding(*, session_id: str | None = "session-reviewed") -> EvaluationBinding:
    return EvaluationBinding(
        suite_id="browser-suite",
        provider="target",
        model="n/a",
        route="refund-browser-v1",
        budget={"case_timeout_seconds": 5, "max_steps": 4},
        dataset_digest="sha256:dataset",
        session_id=session_id,
    )


class _FakeSession:
    def __init__(self, bounds: BrowserBounds, owner: BrowserOwner, *, block: bool = False) -> None:
        self.bounds = bounds
        self.owner = owner
        self.context_id = "context-reviewed"
        self.actions = []
        self.closed = False
        self.started = asyncio.Event()
        self._block = block

    async def act(self, action, *, actor_id: str):
        self.actions.append((action, actor_id))
        if self._block and action.kind == "snapshot":
            self.started.set()
            await asyncio.Future()
        return BrowserObservation(
            context_id=self.context_id,
            target_id=self.bounds.target_id,
            origin=next(iter(self.bounds.allowed_origins)),
            owner=self.owner,
            tab_ref="tab-dynamic-42",
            kind="navigation" if action.kind == "open_tab" else "snapshot",
            step=action.step,
            text="reviewed target ready" if action.kind == "snapshot" else "",
        )

    async def close(self) -> None:
        self.closed = True


def test_browser_fixture_requires_exact_first_origin_and_digest(tmp_path: Path) -> None:
    endpoint = "https://target.test/chat"
    case = _case(tmp_path / "browser.json", endpoint)
    fixture = load_browser_evaluation_fixture(case)
    assert fixture.endpoint == endpoint
    with pytest.raises(ValueError, match="digest mismatch"):
        load_browser_evaluation_fixture(
            EvaluationCase(
                id=case.id,
                target_fixture=case.target_fixture,
                source_group=case.source_group,
                fixture_digest="sha256:" + "0" * 64,
            )
        )


@pytest.mark.asyncio
async def test_browser_evaluator_binds_owner_origin_capture_and_cleanup(tmp_path: Path) -> None:
    endpoint = "https://target.test/chat"
    launched: list[_FakeSession] = []

    async def launch(bounds: BrowserBounds, owner: BrowserOwner):
        session = _FakeSession(bounds, owner)
        launched.append(session)
        return session

    metrics = await BrowserAdapterCaseEvaluator(launch)(
        _candidate(endpoint), _case(tmp_path / "browser.json", endpoint), _binding()
    )
    session = launched[0]
    assert session.owner.session_id == "session-reviewed"
    assert session.bounds.allowed_origins == frozenset({"https://target.test"})
    assert session.actions[1][0].tab_ref == "tab-dynamic-42"
    assert session.closed is True
    assert metrics.task_success_rate == 1
    assert metrics.evidence_attribution_rate == 1
    assert metrics.cancellation_recovery_rate == 1


@pytest.mark.asyncio
async def test_browser_evaluator_requires_session_authority(tmp_path: Path) -> None:
    endpoint = "https://target.test/chat"
    with pytest.raises(PermissionError, match="authorized session"):
        await BrowserAdapterCaseEvaluator()(
            _candidate(endpoint),
            _case(tmp_path / "browser.json", endpoint),
            _binding(session_id=None),
        )


@pytest.mark.asyncio
async def test_browser_evaluator_closes_context_when_cancelled(tmp_path: Path) -> None:
    endpoint = "https://target.test/chat"
    launched: list[_FakeSession] = []
    session_launched = asyncio.Event()

    async def launch(bounds: BrowserBounds, owner: BrowserOwner):
        session = _FakeSession(bounds, owner, block=True)
        launched.append(session)
        session_launched.set()
        return session

    task = asyncio.create_task(
        BrowserAdapterCaseEvaluator(launch)(
            _candidate(endpoint), _case(tmp_path / "browser.json", endpoint), _binding()
        )
    )
    await session_launched.wait()
    await launched[0].started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert launched[0].closed is True


@pytest.mark.asyncio
async def test_authenticated_browser_evaluation_resolves_opaque_profile_and_releases_actor(
    tmp_path: Path,
) -> None:
    endpoint = "https://target.test/chat"
    handle = "browser-profile:configured"
    launched: list[_FakeSession] = []
    released: list[str] = []

    async def resolve(session_id: str, bounds: BrowserBounds):
        assert session_id == "session-reviewed"
        assert bounds.requires_persistent_profile is True
        session = _FakeSession(
            bounds,
            BrowserOwner(
                session_id="existing-profile-owner",
                worker_id="agent:browser",
                technique_id="recon",
            ),
        )
        launched.append(session)
        return session

    async def release(actor_id: str) -> None:
        released.append(actor_id)

    metrics = await BrowserAdapterCaseEvaluator(
        persistent_resolver=resolve,
        actor_releaser=release,
    )(
        _candidate(endpoint, credential_handle=handle),
        _case(tmp_path / "browser-auth.json", endpoint, credential_handle=handle),
        _binding(),
    )
    assert metrics.task_success_rate == 1
    assert launched[0].closed is False
    assert released == ["promotion:candidate-browser"]


@pytest.mark.asyncio
async def test_authenticated_browser_evaluation_rejects_unknown_profile_handle(
    tmp_path: Path,
) -> None:
    endpoint = "https://target.test/chat"
    with pytest.raises(ValueError, match="credential handle is unavailable"):
        await BrowserAdapterCaseEvaluator()(
            _candidate(endpoint, credential_handle="browser-profile:unknown"),
            _case(
                tmp_path / "browser-unknown.json",
                endpoint,
                credential_handle="browser-profile:unknown",
            ),
            _binding(),
        )


@pytest.mark.asyncio
async def test_authenticated_browser_evaluation_releases_actor_when_cancelled(
    tmp_path: Path,
) -> None:
    endpoint = "https://target.test/chat"
    handle = "browser-profile:configured"
    session_ready = asyncio.Event()
    released: list[str] = []
    sessions: list[_FakeSession] = []

    async def resolve(_session_id: str, bounds: BrowserBounds):
        session = _FakeSession(
            bounds,
            BrowserOwner(
                session_id="profile-owner", worker_id="agent:browser", technique_id="recon"
            ),
            block=True,
        )
        sessions.append(session)
        session_ready.set()
        return session

    async def release(actor_id: str) -> None:
        released.append(actor_id)

    task = asyncio.create_task(
        BrowserAdapterCaseEvaluator(
            persistent_resolver=resolve,
            actor_releaser=release,
        )(
            _candidate(endpoint, credential_handle=handle),
            _case(tmp_path / "browser-auth-cancel.json", endpoint, credential_handle=handle),
            _binding(),
        )
    )
    await session_ready.wait()
    await sessions[0].started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert sessions[0].closed is False
    assert released == ["promotion:candidate-browser"]


def _step_for(pointer: str = "") -> BrowserEvaluationStep:
    return BrowserEvaluationStep(
        id="observe",
        tool="browser_snapshot",
        arguments={},
        response_pointer=pointer,
        expected_value=None,
        capture_as=None,
    )


def test_observe_result_parses_fenced_model_reply() -> None:
    content = '```json\n{"text": "reviewed target ready"}\n```'
    assert _observe_result(content, _step_for("/text")) == "reviewed target ready"


def test_observe_result_parses_bare_model_reply() -> None:
    assert _observe_result('{"text": "reviewed target ready"}', _step_for("/text")) == (
        "reviewed target ready"
    )


def test_observe_result_junk_reply_quotes_reply_head() -> None:
    with pytest.raises(ValueError, match="returned invalid JSON") as excinfo:
        _observe_result("not json at all, just prose", _step_for())
    cause = excinfo.value.__cause__
    assert isinstance(cause, ModelJsonError)
    assert "not json at all" in str(cause)
