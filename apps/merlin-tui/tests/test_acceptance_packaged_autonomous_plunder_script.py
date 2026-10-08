from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest


def _script_module():
    path = Path(__file__).parents[3] / "scripts" / "acceptance_packaged_autonomous_plunder.py"
    spec = importlib.util.spec_from_file_location("acceptance_packaged_autonomous_plunder", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _completion_authority(module, marker: str = "stable") -> tuple[dict[str, object], str]:
    document: dict[str, object] = {
        "marker": marker,
        "cleanup": {"authority": {"status": "acknowledged"}},
    }
    digest = (
        "sha256:"
        + module.hashlib.sha256(
            module.json.dumps(document, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
    )
    return document, digest


def test_live_gate_requires_exact_authorization_and_release_binary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = _script_module()
    binary = tmp_path / "merlin"
    binary.write_text("#!/bin/sh\n", encoding="utf-8")
    binary.chmod(0o755)

    monkeypatch.setenv("MERLIN_RUN_LIVE_ACCEPTANCE", "1")
    monkeypatch.setenv("MERLIN_LIVE_AUTHORIZED_TARGET", "https://plunder.me")
    monkeypatch.setenv("MERLIN_LIVE_RELEASE_BINARY", str(binary))
    assert module._release_binary() == binary.resolve()  # noqa: SLF001

    monkeypatch.setenv("MERLIN_LIVE_AUTHORIZED_TARGET", "https://example.test")
    with pytest.raises(RuntimeError, match="exact|requires"):
        module._release_binary()  # noqa: SLF001


def test_main_reports_keyboard_interrupt_without_traceback(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    module = _script_module()

    def interrupted(coroutine):
        coroutine.close()
        raise KeyboardInterrupt

    monkeypatch.setattr(module.asyncio, "run", interrupted)
    monkeypatch.setattr(sys, "argv", ["acceptance_packaged_autonomous_plunder.py"])

    with pytest.raises(SystemExit) as exit_info:
        module.main()

    captured = capsys.readouterr()
    assert exit_info.value.code == 130
    assert captured.out == ""
    assert captured.err == "Acceptance interrupted.\n"
    assert "Traceback" not in captured.err


def test_composition_assertion_requires_autonomous() -> None:
    module = _script_module()
    module._assert_autonomous(  # noqa: SLF001
        {"dynamic_specialist_policy": {"specialist_composition": "autonomous"}}
    )
    with pytest.raises(AssertionError):
        module._assert_autonomous(  # noqa: SLF001
            {"dynamic_specialist_policy": {"specialist_composition": "opinionated"}}
        )


def test_live_objective_is_scoped_to_the_bank_level_one() -> None:
    module = _script_module()

    assert module._assert_scoped_objective(module.LIVE_OBJECTIVE) == module.LIVE_OBJECTIVE  # noqa: SLF001
    with pytest.raises(AssertionError, match="lost required scope"):
        module._assert_scoped_objective(  # noqa: SLF001
            "Run a Merlin scan on plunder.me, Policy Refund Trap Level 1"
        )


def test_report_assertion_allows_no_finding_citations_when_there_are_no_findings() -> None:
    module = _script_module()
    campaign_id = "campaign/example"
    objective = "Assess The Bank on plunder.me, Level 1"
    rows = [
        {
            "campaign_id": campaign_id,
            "revision_id": "report/example",
            "cleanup_status": "completed",
            "report_state": "complete",
            "stop_reason": "saturated",
            "metadata": {
                "specialist_composition": "autonomous",
                "target": module.LIVE_TARGET,
                "objective": objective,
                "completion_digest": "sha256:completion",
            },
            "tasks": [{"id": "task/example"}],
            "techniques": [{"id": "technique/example"}],
            "findings": [],
            "evidence": [],
            "exchanges": [{"request": "hello", "response": "world"}],
        }
    ]

    terminal = {
        "stop_reason": "saturated",
        "completion_digest": "sha256:completion",
    }
    assert module._assert_report_rows(rows, campaign_id, objective, terminal) == (  # noqa: SLF001
        "report/example",
    )


def test_report_assertion_requires_citations_for_findings() -> None:
    module = _script_module()
    campaign_id = "campaign/example"
    objective = "Assess The Bank on plunder.me, Level 1"
    rows = [
        {
            "campaign_id": campaign_id,
            "revision_id": "report/example",
            "cleanup_status": "completed",
            "report_state": "complete",
            "stop_reason": "saturated",
            "metadata": {
                "specialist_composition": "autonomous",
                "target": module.LIVE_TARGET,
                "objective": objective,
                "completion_digest": "sha256:completion",
            },
            "tasks": [{"id": "task/example"}],
            "techniques": [{"id": "technique/example"}],
            "findings": [{"id": "finding/example"}],
            "evidence": [],
            "exchanges": [{"request": "hello", "response": "world"}],
        }
    ]

    with pytest.raises(AssertionError, match="no supporting evidence"):
        module._assert_report_rows(  # noqa: SLF001
            rows,
            campaign_id,
            objective,
            {"stop_reason": "saturated", "completion_digest": "sha256:completion"},
        )


def test_report_assertion_rejects_obsolete_completion_authority() -> None:
    module = _script_module()
    row = {
        "campaign_id": "campaign/example",
        "revision_id": "report/example",
        "cleanup_status": "completed",
        "report_state": "complete",
        "stop_reason": "saturated",
        "metadata": {
            "specialist_composition": "autonomous",
            "target": module.LIVE_TARGET,
            "objective": "objective",
            "completion_digest": "sha256:old",
        },
        "tasks": [{}],
        "techniques": [{}],
        "findings": [],
        "evidence": [],
        "exchanges": [{}],
    }
    with pytest.raises(AssertionError, match="completion evidence"):
        module._assert_report_rows(  # noqa: SLF001
            [row],
            "campaign/example",
            "objective",
            {"stop_reason": "saturated", "completion_digest": "sha256:new"},
        )


@pytest.mark.asyncio
async def test_acceptance_watchdog_is_a_direct_owned_child_and_is_reaped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _script_module()
    launched: list[tuple[object, ...]] = []
    lifecycle: list[str] = []

    class Process:
        returncode = None

        def terminate(self) -> None:
            lifecycle.append("terminate")

        def kill(self) -> None:
            raise AssertionError("a responsive watchdog must not be killed")

        async def wait(self) -> int:
            lifecycle.append("wait")
            self.returncode = -15
            return self.returncode

    process = Process()

    async def create_subprocess_exec(*arguments: object, **kwargs: object) -> Process:
        assert not kwargs
        launched.append(arguments)
        return process

    monkeypatch.setattr(module.asyncio, "create_subprocess_exec", create_subprocess_exec)

    observed = await module._start_owned_watchdog(  # noqa: SLF001
        Path("/release/merlin"), "campaign/owned"
    )
    await module._stop_owned_watchdog(observed)  # noqa: SLF001

    assert launched == [
        (
            sys.executable,
            str(Path(module.__file__).with_name("campaign_watchdog.py")),
            "--campaign",
            "campaign/owned",
            "--interval",
            "30",
            "--fail-on-stall",
            "--stall-confirmations",
            "2",
            "--merlin",
            "/release/merlin",
        )
    ]
    assert lifecycle == ["terminate", "wait"]


@pytest.mark.asyncio
async def test_terminal_timeout_preserves_owned_campaign_for_scoped_pause() -> None:
    module = _script_module()

    class Client:
        async def campaign_status(self, campaign_id: str):
            return {
                "campaign_id": campaign_id,
                "status": "executing",
                "dynamic_specialist_policy": {"specialist_composition": "autonomous"},
            }

    client = Client()
    with pytest.raises(AssertionError, match="preserved for scoped pause"):
        await module._wait_for_terminal(  # noqa: SLF001
            client,
            "campaign/owned",
            timeout_seconds=0.001,
            max_extensions=0,
        )


@pytest.mark.asyncio
async def test_terminal_wait_aborts_when_watchdog_reports_confirmed_stall() -> None:
    module = _script_module()

    class Client:
        async def campaign_status(self, _campaign_id: str):
            raise AssertionError("watchdog failure must be checked before another status poll")

    class Process:
        returncode = 3

    with pytest.raises(AssertionError, match="watchdog failed"):
        await module._wait_for_terminal(  # noqa: SLF001
            Client(),
            "campaign/owned",
            timeout_seconds=1,
            max_extensions=0,
            watchdog=Process(),
        )


@pytest.mark.asyncio
async def test_terminal_wait_requires_stable_canonical_authority(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _script_module()
    old_document, old_digest = _completion_authority(module, "old")
    new_document, new_digest = _completion_authority(module, "new")

    class Client:
        def __init__(self) -> None:
            self.statuses = iter(
                (
                    {
                        "status": "completed",
                        "stop_reason": "saturated",
                        "completion_digest": old_digest,
                        "completion_document": old_document,
                    },
                    {"status": "executing", "stop_reason": None, "completion_digest": None},
                    {
                        "status": "completed",
                        "stop_reason": "saturated",
                        "completion_digest": new_digest,
                        "completion_document": new_document,
                    },
                    {
                        "status": "completed",
                        "stop_reason": "saturated",
                        "completion_digest": new_digest,
                        "completion_document": new_document,
                    },
                )
            )

        async def campaign_status(self, campaign_id: str):
            return {
                "campaign_id": campaign_id,
                "dynamic_specialist_policy": {"specialist_composition": "autonomous"},
                **next(self.statuses),
            }

    async def no_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(module.asyncio, "sleep", no_sleep)
    terminal = await module._wait_for_terminal(  # noqa: SLF001
        Client(), "campaign/owned", timeout_seconds=1, max_extensions=0
    )
    assert terminal["completion_digest"] == new_digest


def test_acceptance_rejects_failed_watchdog() -> None:
    module = _script_module()

    class Process:
        returncode = 2

    with pytest.raises(AssertionError, match="watchdog failed"):
        module._assert_watchdog_healthy(Process())  # noqa: SLF001


def test_terminal_campaign_requires_canonical_cleanup_acknowledgement() -> None:
    module = _script_module()
    campaign_id = "campaign/owned"
    completion_document = {
        "campaign_id": campaign_id,
        "profile": "recommended",
        "target_id": "target/one",
        "cleanup": {"authority": {"status": "unresolved"}},
        "saturation": {"execution_complete": True},
    }
    completion_digest = (
        "sha256:"
        + module.hashlib.sha256(
            module.json.dumps(completion_document, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
    )
    status = {
        "campaign_id": campaign_id,
        "status": "completed",
        "stop_reason": "saturated",
        "lead_events": 1,
        "dynamic_specialist_policy": {"specialist_composition": "autonomous"},
        "dynamic_specialists": [{"id": "specialist/one"}],
        "waves": [{"tasks": [{"status": "completed"}]}],
        "completion_digest": completion_digest,
        "completion_document": completion_document,
    }

    with pytest.raises(AssertionError, match="cleanup lacks canonical acknowledgement"):
        module._assert_terminal_campaign(status, campaign_id)  # noqa: SLF001

    status["completion_document"]["cleanup"]["authority"]["status"] = "acknowledged"
    status["completion_digest"] = (
        "sha256:"
        + module.hashlib.sha256(
            module.json.dumps(
                status["completion_document"], sort_keys=True, separators=(",", ":")
            ).encode()
        ).hexdigest()
    )
    module._assert_terminal_campaign(status, campaign_id)  # noqa: SLF001


@pytest.mark.parametrize("completion_digest", (None, "bogus"))
def test_terminal_campaign_rejects_missing_or_bogus_completion_digest(
    completion_digest: object,
) -> None:
    module = _script_module()
    completion_document, _valid_digest = _completion_authority(module)
    with pytest.raises(AssertionError, match="digest is missing or malformed"):
        module._assert_terminal_completion_authority(  # noqa: SLF001
            {
                "completion_digest": completion_digest,
                "completion_document": completion_document,
            }
        )


def test_terminal_campaign_rejects_completion_document_digest_drift() -> None:
    module = _script_module()
    completion_document, digest = _completion_authority(module)
    completion_document["marker"] = "mutated"
    with pytest.raises(AssertionError, match="does not match its digest"):
        module._assert_terminal_completion_authority(  # noqa: SLF001
            {
                "completion_digest": digest,
                "completion_document": completion_document,
            }
        )


@pytest.mark.asyncio
async def test_interrupted_acceptance_pauses_owned_executing_campaign() -> None:
    module = _script_module()

    class Client:
        def __init__(self) -> None:
            self.paused: list[str] = []

        async def campaign_status(self, campaign_id: str):
            return {"campaign_id": campaign_id, "status": "executing"}

        async def pause_campaign(self, campaign_id: str):
            self.paused.append(campaign_id)

        async def cancel_campaign(self, _campaign_id: str):
            raise AssertionError("executing campaign must remain resumable")

    client = Client()
    await module._pause_owned_campaign(client, "campaign/owned")  # noqa: SLF001
    assert client.paused == ["campaign/owned"]


@pytest.mark.asyncio
async def test_interrupted_acceptance_accepts_concurrent_pause_race() -> None:
    module = _script_module()

    class Client:
        def __init__(self) -> None:
            self.statuses = iter(("executing", "paused"))

        async def campaign_status(self, campaign_id: str):
            return {"campaign_id": campaign_id, "status": next(self.statuses)}

        async def pause_campaign(self, _campaign_id: str):
            raise RuntimeError("campaign wave is not running")

    await module._pause_owned_campaign(Client(), "campaign/owned")  # noqa: SLF001


@pytest.mark.asyncio
async def test_interrupted_acceptance_cancels_owned_prepared_campaign() -> None:
    module = _script_module()

    class Client:
        def __init__(self) -> None:
            self.cancelled: list[str] = []

        async def campaign_status(self, campaign_id: str):
            return {"campaign_id": campaign_id, "status": "prepared"}

        async def cancel_campaign(self, campaign_id: str):
            self.cancelled.append(campaign_id)

        async def pause_campaign(self, _campaign_id: str):
            raise AssertionError("prepared campaign has no executable work to pause")

    client = Client()
    await module._pause_owned_campaign(client, "campaign/owned")  # noqa: SLF001
    assert client.cancelled == ["campaign/owned"]


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ("paused", "completed", "failed", "cancelled"))
async def test_interrupted_acceptance_leaves_inactive_campaign_unchanged(status: str) -> None:
    module = _script_module()

    class Client:
        async def campaign_status(self, campaign_id: str):
            return {"campaign_id": campaign_id, "status": status}

        async def cancel_campaign(self, _campaign_id: str):
            raise AssertionError("inactive campaign must not be cancelled again")

        async def pause_campaign(self, _campaign_id: str):
            raise AssertionError("inactive campaign must not be paused again")

    await module._pause_owned_campaign(Client(), "campaign/owned")  # noqa: SLF001


@pytest.mark.asyncio
async def test_interrupted_acceptance_propagates_persistent_pause_error() -> None:
    module = _script_module()

    class Client:
        async def campaign_status(self, campaign_id: str):
            return {"campaign_id": campaign_id, "status": "executing"}

        async def pause_campaign(self, _campaign_id: str):
            raise RuntimeError("storage unavailable")

    with pytest.raises(RuntimeError, match="storage unavailable"):
        await module._pause_owned_campaign(Client(), "campaign/owned")  # noqa: SLF001
