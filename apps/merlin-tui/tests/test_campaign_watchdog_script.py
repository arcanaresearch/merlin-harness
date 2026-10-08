from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


def _script_module():
    path = Path(__file__).parents[3] / "scripts" / "campaign_watchdog.py"
    spec = importlib.util.spec_from_file_location("campaign_watchdog", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _run_watchdog(monkeypatch, stalled_states: list[bool]) -> tuple[int, int]:
    module = _script_module()
    polls = iter(stalled_states)
    observed = 0

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "campaign_watchdog.py",
            "--campaign",
            "campaign/owned",
            "--interval",
            "1",
            "--stall-after",
            "1",
            "--fail-on-stall",
            "--stall-confirmations",
            "2",
        ],
    )
    monkeypatch.setattr(module, "_resolve_campaign", lambda *_args: "campaign/owned")
    monkeypatch.setattr(
        module,
        "_campaign_row",
        lambda *_args: {
            "id": "campaign/owned",
            "status": "executing",
            "stop_reason": "",
            "updated_age_s": "10",
            "composition": "autonomous",
        },
    )
    monkeypatch.setattr(module, "_last_journal", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(module, "_specialists", lambda *_args: [])
    monkeypatch.setattr(module, "_attempts", lambda *_args: [])
    monkeypatch.setattr(module, "snapshot", lambda *_args: None)
    monkeypatch.setattr(module.time, "sleep", lambda _seconds: None)

    def stall_state(*_args):
        nonlocal observed
        observed += 1
        return next(polls), "10"

    monkeypatch.setattr(module, "stall_state", stall_state)
    return module.main(), observed


def test_watchdog_exits_after_two_confirmed_stall_polls(monkeypatch, capsys) -> None:
    result, observed = _run_watchdog(monkeypatch, [True, True])

    assert result == 3
    assert observed == 2
    assert "CONFIRMED_STALL" in capsys.readouterr().out


def test_watchdog_resets_confirmation_after_transient_progress(monkeypatch) -> None:
    result, observed = _run_watchdog(monkeypatch, [True, False, True, True])

    assert result == 3
    assert observed == 4


def test_fresh_lease_heartbeat_does_not_hide_semantic_stall(monkeypatch) -> None:
    module = _script_module()
    monkeypatch.setattr(
        module,
        "_campaign_row",
        lambda *_args: {
            "id": "campaign/owned",
            "status": "executing",
            "stop_reason": "",
            "updated_age_s": "600",
            "composition": "autonomous",
        },
    )
    monkeypatch.setattr(module, "_last_journal", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(module, "_scheduler_activity_age", lambda *_args: "601")
    monkeypatch.setattr(module, "_attempts", lambda *_args: ["running\t1\t0"])

    assert module.stall_state("campaign/owned", "merlin", 300) == (True, "600")


def test_recent_non_heartbeat_scheduler_event_is_semantic_progress(monkeypatch) -> None:
    module = _script_module()
    monkeypatch.setattr(
        module,
        "_campaign_row",
        lambda *_args: {
            "id": "campaign/owned",
            "status": "executing",
            "stop_reason": "",
            "updated_age_s": "600",
            "composition": "autonomous",
        },
    )
    monkeypatch.setattr(module, "_last_journal", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(module, "_scheduler_activity_age", lambda *_args: "10")

    assert module.stall_state("campaign/owned", "merlin", 300) == (False, "10")


def test_attempt_query_includes_root_and_campaign_wave_schedules(monkeypatch) -> None:
    module = _script_module()
    observed: list[str] = []

    def run_sql(sql: str, _merlin: str) -> list[str]:
        observed.append(sql)
        return ["running\t1\t3"]

    monkeypatch.setattr(module, "run_sql", run_sql)

    assert module._attempts("campaign/owned", "merlin") == ["running\t1\t3"]  # noqa: SLF001
    (sql,) = observed
    assert "FROM merlin_storage.assessment_campaigns" in sql
    assert "FROM merlin_storage.campaign_waves" in sql
    assert "JOIN campaign_schedules s ON s.schedule_id = a.schedule_id" in sql


def test_activity_query_includes_root_and_campaign_wave_schedules(monkeypatch) -> None:
    module = _script_module()
    observed: list[str] = []

    def run_sql(sql: str, _merlin: str) -> list[str]:
        observed.append(sql)
        return ["7"]

    monkeypatch.setattr(module, "run_sql", run_sql)

    assert module._scheduler_activity_age("campaign/owned", "merlin") == "7"  # noqa: SLF001
    (sql,) = observed
    assert "FROM merlin_storage.assessment_campaigns" in sql
    assert "FROM merlin_storage.campaign_waves" in sql
    assert "JOIN campaign_schedules s ON s.schedule_id = e.schedule_id" in sql
    assert "WHERE e.type <> 'heartbeat'" in sql
