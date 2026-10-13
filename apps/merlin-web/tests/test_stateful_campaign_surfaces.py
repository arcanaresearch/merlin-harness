from __future__ import annotations

import asyncio
import json
import sys
from collections.abc import Mapping
from dataclasses import asdict
from pathlib import Path

import httpx
import pytest
from aiohttp import ClientSession, CookieJar, web
from merlin_assessments import (
    ReviewedApiTargetTool,
    StatefulFixtureBudget,
    StatefulTargetFixtureServer,
    diagnose_stateful_campaign,
)
from merlin_config import ConfigPaths, CredentialCheckpointVault
from merlin_contracts import ToolContext
from merlin_contracts.target_adapter_compiler import (
    compile_target_adapter,
    compiled_target_adapter_digest,
)
from merlin_storage_postgres import (
    CampaignApiSessionRepository,
    CampaignStore,
    Database,
    DatabaseConfig,
    MigrationRunner,
    SessionRepository,
)
from merlin_tui.app import MerlinApp
from merlin_tui.backend import CampaignView, DemoBackend, StreamEvent
from merlin_tui.cli import main as cli_main
from merlin_web.server import Workspace, create_app

ROOT = Path(__file__).resolve().parents[3]
FIXTURES = ROOT / "packages" / "merlin-assessments" / "tests" / "fixtures"
SNAPSHOT_PATH = FIXTURES / "stateful_failed_campaign_snapshot.json"
CORPUS_PATH = FIXTURES / "stateful_target_regressions.json"
CORPUS = json.loads(CORPUS_PATH.read_text(encoding="utf-8"))
MATRIX_CASES = tuple(CORPUS["cases"])


def _prepare(connection: object, campaign_id: str) -> CampaignApiSessionRepository:
    session = SessionRepository(connection).create("Stateful public surface acceptance")
    CampaignStore(connection).prepare(
        campaign_id,
        str(session.id),
        {"manifest": {"id": campaign_id}, "assignment_context": {}},
        "sha256:reviewed",
    )
    return CampaignApiSessionRepository(connection)


def _context(campaign_id: str, adapter: dict[str, object], call: str = "1") -> ToolContext:
    compilation = compile_target_adapter(adapter)
    return ToolContext(
        session_id="00000000-0000-0000-0000-000000000001",
        workspace_root="/workspace",
        call_id=f"call-{call}",
        agent_id=f"{campaign_id}/candidate-1",
        campaign_id=campaign_id,
        target_id="generic-stateful-target",
        compiled_adapter=json.dumps(
            compilation.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
        ),
        compiled_adapter_digest=compiled_target_adapter_digest(compilation),
    )


async def _invoke(
    target: StatefulTargetFixtureServer,
    campaign_id: str,
    data_dir: Path,
    *inputs: str,
) -> tuple[list[object], list[str]]:
    adapter = target.adapter()
    with Database(DatabaseConfig(data_dir=data_dir)) as database:
        with database.connect() as connection:
            MigrationRunner(connection).apply()
            sessions = _prepare(connection, campaign_id)
            async with httpx.AsyncClient() as client:
                results = []
                for index, value in enumerate(inputs, start=1):
                    tool = ReviewedApiTargetTool(
                        sessions,
                        lambda _: adapter,
                        {},
                        checkpoint_vault=CredentialCheckpointVault(
                            ConfigPaths(data_dir / "credentials")
                        ),
                        client=client,
                    )
                    results.append(
                        await tool.invoke(
                            _context(campaign_id, adapter, str(index)), {"input": value}
                        )
                    )
    return results, [str(item["operation"]) for item in target.http_log]


def _has_keyed_digest(value: object) -> bool:
    if isinstance(value, Mapping):
        return value.get("kind") == "keyed_digest" or any(
            _has_keyed_digest(item) for item in value.values()
        )
    if isinstance(value, (tuple, list)):
        return any(_has_keyed_digest(item) for item in value)
    return False


async def _run_concurrent_campaign_case(data_dir: Path) -> dict[str, object]:
    async with StatefulTargetFixtureServer(
        maximum_parallel_states=1, interaction_delay_seconds=0.1
    ) as target:
        adapter = target.adapter()
        campaign_ids = (
            "campaign/fixture/concurrent-campaign-shared-state/a",
            "campaign/fixture/concurrent-campaign-shared-state/b",
        )
        with Database(DatabaseConfig(data_dir=data_dir)) as database:
            with database.connect() as connection:
                MigrationRunner(connection).apply()
                sessions = _prepare(connection, campaign_ids[0])
                _prepare(connection, campaign_ids[1])
                async with httpx.AsyncClient() as client:
                    tool = ReviewedApiTargetTool(
                        sessions,
                        lambda _: adapter,
                        {},
                        checkpoint_vault=CredentialCheckpointVault(
                            ConfigPaths(data_dir / "credentials")
                        ),
                        client=client,
                    )
                    first_task = asyncio.create_task(
                        tool.invoke(_context(campaign_ids[0], adapter), {"input": "first"})
                    )
                    await asyncio.sleep(0.02)
                    second = await tool.invoke(
                        _context(campaign_ids[1], adapter), {"input": "second"}
                    )
                    first = await first_task
        metrics = target.metrics()
    results = (first, second)
    return {
        "accepted": sum(result.is_error is False for result in results),
        "blocked": sum(
            result.metadata.get("attempt_disposition") == "infrastructure_failed"
            for result in results
        ),
        "target_interactions": metrics.interactions,
    }


async def _run_fresh_state_case(data_dir: Path) -> dict[str, object]:
    async with StatefulTargetFixtureServer(state_mode="fresh") as target:
        adapter = target.adapter()
        campaign_ids = (
            "campaign/fixture/fresh-independent-state/a",
            "campaign/fixture/fresh-independent-state/b",
        )
        with Database(DatabaseConfig(data_dir=data_dir)) as database:
            with database.connect() as connection:
                MigrationRunner(connection).apply()
                sessions = _prepare(connection, campaign_ids[0])
                _prepare(connection, campaign_ids[1])
                async with httpx.AsyncClient() as client:
                    tool = ReviewedApiTargetTool(
                        sessions,
                        lambda _: adapter,
                        {},
                        checkpoint_vault=CredentialCheckpointVault(
                            ConfigPaths(data_dir / "credentials")
                        ),
                        client=client,
                    )
                    results = [
                        await tool.invoke(_context(campaign_id, adapter), {"input": "advance"})
                        for campaign_id in campaign_ids
                    ]
        metrics = target.metrics()
    return {
        "accepted": sum(result.is_error is False for result in results),
        "physical_states": metrics.physical_states,
        "identity_collisions": metrics.identity_collisions,
    }


async def _run_case(case: Mapping[str, object], data_dir: Path) -> dict[str, object]:
    kind = str(case["kind"])
    campaign_id = _campaign_id(case)
    if kind == "shared_state":
        async with StatefulTargetFixtureServer(maximum_parallel_states=2, input_limit=5) as target:
            async with httpx.AsyncClient(base_url=target.origin) as client:
                starts = [await client.post("/api/start", json={}) for _ in range(16)]
                leases = [response.json()["leaseId"] for response in starts]
                for index in range(14):
                    await client.post(
                        "/api/message",
                        json={"leaseId": leases[index], "message": "short"},
                    )
                for index in range(4):
                    await client.post(
                        "/api/message",
                        json={"leaseId": leases[index], "message": "too-long"},
                    )
            metrics = target.metrics()
        return {
            "identity_collisions": metrics.identity_collisions,
            "negative_learning_allowed": False,
            "retry_required": metrics.identity_collisions > 0,
        }
    if kind == "concurrent_campaign":
        return await _run_concurrent_campaign_case(data_dir)
    if kind == "fresh_state":
        return await _run_fresh_state_case(data_dir)

    options: dict[str, object] = {}
    inputs = ("advance",)
    if kind == "empty_state":
        options["include_state_capabilities"] = False
    elif kind == "stateful_chat":
        options["state_mode"] = "fresh"
    elif kind == "ordered_workflow":
        options.update(maximum_parallel_states=1, state_mode="checkpoint")
    elif kind == "rotating_ids":
        options.update(state_mode="fresh", rotate_identity_on_message=True)
    elif kind == "hostile_oracle":
        options.update(state_mode="fresh", hostile_oracle=True)
    elif kind == "quota":
        options.update(maximum_parallel_states=1, interaction_quota=1)
        inputs = ("one", "two")
    elif kind == "multi_message":
        options["multi_message"] = True
    elif kind == "restart":
        options["maximum_parallel_states"] = 1
        inputs = ("before restart", "after restart")
    elif kind == "constraint":
        options["input_limit"] = 5
        inputs = ("message exceeds the reviewed limit",)
    elif kind == "sensitive_identity":
        options["state_mode"] = "fresh"
    elif kind == "missing_oracle":
        options["include_observations"] = False
    else:
        raise AssertionError(f"unhandled fixture kind: {kind}")

    async with StatefulTargetFixtureServer(**options) as target:
        results, operations = await _invoke(target, campaign_id, data_dir, *inputs)
        metrics = target.metrics()
    first = results[0]
    metadata = first.metadata
    if kind == "empty_state":
        return {
            "outcome": "unclear",
            "execution_allowed": first.is_error is False
            and "target_state_transition" not in metadata,
        }
    if kind == "stateful_chat":
        transition = metadata["target_state_transition"]
        return {"outcome": transition["outcome"], "authority": transition["authority"]}
    if kind == "ordered_workflow":
        return {"steps": operations[:3]}
    if kind == "rotating_ids":
        transition = metadata["target_state_transition"]
        return {
            "disposition": metadata["attempt_disposition"],
            "outcome": transition["outcome"],
        }
    if kind == "hostile_oracle":
        return {
            "untrusted_claim_authoritative": metadata.get("target_authoritative_outcome")
            == "confirmed"
        }
    if kind == "quota":
        second = results[1]
        return {
            "status": (
                429 if second.metadata.get("failure_code") == "target_resource_blocked" else 200
            ),
            "quality_penalty": second.metadata.get("attempt_disposition")
            != "infrastructure_failed",
        }
    if kind == "multi_message":
        return {"messages_captured": len(json.loads(first.content))}
    if kind == "constraint":
        return {
            "disposition": metadata.get("attempt_disposition"),
            "target_calls": metrics.requests,
        }
    if kind == "sensitive_identity":
        serialized = json.dumps(metadata, sort_keys=True)
        return {
            "raw_identity_exposed": "physical-" in serialized or "principal-" in serialized,
            "keyed_digest_present": _has_keyed_digest(metadata),
        }
    if kind == "missing_oracle":
        transition = metadata.get("target_state_transition")
        return {
            "outcome": (
                transition.get("outcome") if isinstance(transition, Mapping) else "unclear"
            ),
            "authoritative": metadata.get("outcome_authority") == "target",
        }
    return {"physical_identity_preserved": metrics.starts == 1 and metrics.physical_states == 1}


async def _status(case: dict[str, object], data_dir: Path) -> dict[str, object]:
    snapshot = json.loads(SNAPSHOT_PATH.read_text(encoding="utf-8"))
    budget = StatefulFixtureBudget(**snapshot["fixture_budget"])
    diagnostics = diagnose_stateful_campaign(snapshot, budget=budget)
    case_id = str(case["id"])
    observed = await _run_case(case, data_dir)
    expected = case["expected"]
    return {
        "campaign_id": f"campaign/fixture/{case_id}",
        "status": "completed",
        "stop_reason": None,
        "manifest_digest": "sha256:sanitized",
        "stateful_acceptance": {
            "schema": CORPUS["schema"],
            "case_id": case_id,
            "kind": case["kind"],
            "result": "passed" if observed == expected else "failed",
            "expected": expected,
            "observed": observed,
        },
        "stateful_diagnostics": asdict(diagnostics),
    }


class FixtureMatrixBackend(DemoBackend):
    def __init__(self, data_dir: Path) -> None:
        super().__init__()
        self._data_dir = data_dir
        self._cases = {str(case["id"]): case for case in MATRIX_CASES}
        self.status_requests: list[str] = []
        self.active_case_id = ""
        self.prepared = False
        self.executions: list[tuple[str, str]] = []
        self.result: dict[str, object] | None = None

    def _review(self, case_id: str) -> dict[str, object]:
        return {
            "campaign_id": f"campaign/fixture/{case_id}",
            "manifest_digest": "sha256:sanitized",
            "objective": f"campaign/fixture/{case_id}",
            "profile": "recommended",
            "campaign_lead": 1,
            "specialist_team": 1,
            "maximum_concurrency": 2,
            "review": {
                "budget": {"turns": None, "tokens": None, "seconds": None, "cost_usd": None},
                "admitted_coverage": {"technique_count": 1, "technique_ids": ["fixture"]},
                "language_plan": {
                    "baseline": "en",
                    "policy": "fixture baseline",
                    "language_evasion_cells": [],
                },
                "hypothesis_reserve": {"required_cells": 0, "rule": "none"},
                "capability_projection": {
                    "packs": [],
                    "skills": [],
                    "mcp_tools": [],
                    "tools": ["target_request"],
                    "authority": ["target-read", "target-write"],
                },
                "target_contract": {
                    "interface": "http_api",
                    "endpoint": "http://127.0.0.1/fixture",
                    "adapter_id": "stateful-fixture-v1",
                    "session_constraint": None,
                    "api_validated": True,
                    "execution_mode": "api",
                    "execution_options": ["api"],
                },
                "recon_evidence": {
                    "references": ["fixture-corpus"],
                    "input_locator": "request.input",
                    "submit_action": "http",
                    "success_indicator": "reviewed transition oracle",
                },
                "provider_route": "fixture-provider",
                "provider_model": "fixture-model",
                "model_routes": {},
                "model_route_details": {},
                "model_escalations": {},
                "specialist_composition": "opinionated",
                "authentication": "anonymous",
                "benign_controls": [],
                "specialist_routing": {"fixture": ["fixture"]},
                "stopping_policy": "complete the fixture case",
                "cleanup_obligations": ["close fixture state"],
                "known_limitations": [],
                "approval_binding": "Exact manifest digest",
            },
        }

    async def stream(self, prompt: str):
        if prompt == "/new":
            yield StreamEvent("status", "new session")
            return
        case_id = next((item for item in self._cases if item in prompt), "")
        if not case_id:
            raise ValueError("stateful fixture case missing from preparation prompt")
        self.active_case_id = case_id
        self.prepared = True
        yield StreamEvent("campaign_review", json.dumps(self._review(case_id)))

    async def list_campaigns(self) -> tuple[CampaignView, ...]:
        if not self.prepared:
            return ()
        return (
            CampaignView(
                id=f"campaign/fixture/{self.active_case_id}",
                session_id="00000000-0000-0000-0000-000000000001",
                objective=f"campaign/fixture/{self.active_case_id}",
                profile="recommended",
                status="prepared" if self.result is None else "completed",
                manifest_digest="sha256:sanitized",
            ),
        )

    async def execute_campaign(self, campaign_id: str, manifest_digest: str) -> object:
        expected_id = f"campaign/fixture/{self.active_case_id}"
        if not self.prepared or campaign_id != expected_id:
            raise ValueError("fixture campaign was not prepared through the public interface")
        if manifest_digest != "sha256:sanitized":
            raise ValueError("fixture manifest digest changed before execution")
        self.executions.append((campaign_id, manifest_digest))
        self.result = await _status(
            self._cases[self.active_case_id], self._data_dir / self.active_case_id
        )
        return self.result

    async def campaign_status(self, campaign_id: str) -> object:
        self.status_requests.append(campaign_id)
        prefix = "campaign/fixture/"
        case_id = campaign_id.removeprefix(prefix)
        if not campaign_id.startswith(prefix) or case_id not in self._cases:
            raise ValueError(f"unknown stateful fixture campaign: {campaign_id}")
        if self.result is not None:
            return self.result
        if self.prepared and case_id == self.active_case_id:
            return {
                **self._review(case_id),
                "status": "prepared",
                "specialist_composition": "opinionated",
                "budget": {"limits": {}},
            }
        raise ValueError("fixture campaign has not been prepared")


def _campaign_id(case: dict[str, object]) -> str:
    return f"campaign/fixture/{case['id']}"


def _assert_case_payload(payload: dict[str, object], case: dict[str, object]) -> None:
    acceptance = payload["stateful_acceptance"]
    assert isinstance(acceptance, dict)
    assert acceptance == {
        "schema": CORPUS["schema"],
        "case_id": case["id"],
        "kind": case["kind"],
        "result": "passed",
        "expected": case["expected"],
        "observed": case["expected"],
    }


@pytest.mark.parametrize("case", MATRIX_CASES, ids=lambda case: str(case["id"]))
def test_command_interface_exposes_every_stateful_fixture_case(
    case: dict[str, object],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    backend = FixtureMatrixBackend(tmp_path)

    async def build_client(*, confirmer: object | None = None) -> FixtureMatrixBackend:
        return backend

    async def daemon_ready() -> bool:
        return False

    campaign_id = _campaign_id(case)
    monkeypatch.setattr("merlin_tui.cli._client", build_client)
    monkeypatch.setattr("merlin_tui.local_control.LocalControlClient", lambda **_: backend)
    monkeypatch.setattr("merlin_tui.local_control.ensure_daemon", daemon_ready)
    monkeypatch.setattr(
        sys,
        "argv",
        ["merlin", "campaign", "submit", "--approve", campaign_id],
    )

    cli_main()

    output = capsys.readouterr().out
    assert f"Campaign started: {campaign_id}" in output
    assert backend.result is not None
    _assert_case_payload(backend.result, case)
    assert backend.executions == [(campaign_id, "sha256:sanitized")]


@pytest.mark.asyncio
@pytest.mark.parametrize("case", MATRIX_CASES, ids=lambda case: str(case["id"]))
async def test_tui_command_exposes_every_stateful_fixture_case(
    case: dict[str, object], tmp_path: Path
) -> None:
    backend = FixtureMatrixBackend(tmp_path)
    app = MerlinApp(backend)
    campaign_id = _campaign_id(case)

    async with app.run_test() as pilot:
        app.query_one("#composer").text = f"prepare {campaign_id}"
        await app.action_send()
        for _ in range(100):
            await pilot.pause()
            if app._pending_campaign_review is not None:
                break
            await asyncio.sleep(0.05)
        assert app._pending_campaign_review == (campaign_id, "sha256:sanitized")

        app.query_one("#composer").text = ""
        await app.action_send()
        for _ in range(100):
            await pilot.pause()
            if backend.result is not None:
                break
            await asyncio.sleep(0.05)

        assert backend.result is not None
        _assert_case_payload(backend.result, case)

    assert backend.executions == [(campaign_id, "sha256:sanitized")]


@pytest.mark.asyncio
@pytest.mark.parametrize("case", MATRIX_CASES, ids=lambda case: str(case["id"]))
async def test_authenticated_web_command_exposes_every_stateful_fixture_case(
    case: dict[str, object], tmp_path: Path
) -> None:
    static = tmp_path / "static"
    static.mkdir()
    (static / "index.html").write_text("fixture", encoding="utf-8")
    backend = FixtureMatrixBackend(tmp_path)
    workspace = Workspace(backend)
    runner = web.AppRunner(create_app(workspace, static))
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    assert site._server is not None and site._server.sockets
    port = site._server.sockets[0].getsockname()[1]
    origin = f"http://127.0.0.1:{port}"
    campaign_id = _campaign_id(case)
    try:
        async with ClientSession(cookie_jar=CookieJar(unsafe=True)) as browser:
            connected = await browser.post(origin + "/api/connect", json={"token": workspace.token})
            assert connected.status == 200
            submitted = await browser.post(
                origin + "/api/chat",
                json={
                    "prompt": f"prepare {campaign_id}",
                    "request_id": f"stateful-{case['id']}",
                },
            )
            assert submitted.status == 200
            if workspace.turn is not None:
                await workspace.turn
            execution = await browser.post(
                origin + "/api/rpc",
                json={
                    "method": "execute_campaign",
                    "args": [campaign_id, "sha256:sanitized"],
                },
            )
            assert execution.status == 200
            payload = await execution.json()
    finally:
        await runner.cleanup()

    status = payload["value"]
    _assert_case_payload(status, case)
    diagnostics = status["stateful_diagnostics"]
    assert diagnostics["metrics"]["contaminated_attempts"] == 14
    assert diagnostics["metrics"]["transition_disagreements"] == 1
    assert diagnostics["terminal_layer_disagreement"] is True
    assert diagnostics["budget_failures"] == [
        "total_tokens",
        "unique_valid_attempts",
        "accepted_attempts_per_minute",
    ]
    assert backend.executions == [(campaign_id, "sha256:sanitized")]
