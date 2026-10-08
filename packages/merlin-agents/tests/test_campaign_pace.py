from datetime import UTC, datetime, timedelta

from merlin_agents.campaign_service import _attempt_duration_seconds, _campaign_pace

NOW = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)


def _wave(wave, status, started, finished=None, tasks=()):
    return {
        "wave": wave,
        "status": status,
        "started_at": started,
        "finished_at": finished,
        "tasks": tuple(tasks),
    }


def test_pace_reports_estimating_until_a_completed_task_carries_timing() -> None:
    pace = _campaign_pace(
        (
            _wave(
                0,
                "running",
                NOW - timedelta(seconds=30),
                tasks=(("completed", None), ("running", None), ("queued", None)),
            ),
        ),
        NOW,
    )

    current = pace["current_wave"]
    assert current["tasks_completed"] == 1
    assert current["tasks_total"] == 3
    assert current["avg_task_seconds"] is None
    assert current["eta_seconds"] is None
    assert pace["min_remaining_seconds"] is None


def test_pace_current_wave_eta_is_remaining_tasks_times_average_duration() -> None:
    started = NOW - timedelta(seconds=100)
    pace = _campaign_pace(
        (
            _wave(
                1,
                "running",
                started,
                tasks=(
                    ("completed", 10.0),
                    ("failed", 20.0),
                    ("running", None),
                    ("queued", None),
                    ("queued", None),
                ),
            ),
        ),
        NOW,
    )

    current = pace["current_wave"]
    assert current["wave"] == 1
    assert current["tasks_completed"] == 2
    assert current["tasks_total"] == 5
    assert current["avg_task_seconds"] == 15.0
    assert current["eta_seconds"] == 45.0
    assert current["elapsed_seconds"] == 100.0
    assert pace["min_remaining_seconds"] == 45.0


def test_pace_campaign_stats_cover_elapsed_waves_and_average_wave_duration() -> None:
    wave0_start = NOW - timedelta(seconds=600)
    wave0_end = wave0_start + timedelta(seconds=240)
    wave1_start = wave0_end
    wave1_end = wave1_start + timedelta(seconds=120)
    pace = _campaign_pace(
        (
            _wave(
                0,
                "completed",
                wave0_start,
                wave0_end,
                tasks=(("completed", 30.0), ("completed", 50.0)),
            ),
            _wave(1, "completed", wave1_start, wave1_end, tasks=(("completed", 60.0),)),
            _wave(2, "running", wave1_end, tasks=(("running", None),)),
        ),
        NOW,
    )

    assert pace["elapsed_seconds"] == 600.0
    assert pace["waves_completed"] == 2
    assert pace["avg_wave_seconds"] == 180.0
    # The most recent completed wave, not an average across waves.
    assert pace["last_wave_avg_task_seconds"] == 60.0
    # No completed task in the running wave: the lower bound stays unknown.
    assert pace["min_remaining_seconds"] is None


def test_pace_without_a_running_wave_has_no_current_wave_or_bound() -> None:
    start = NOW - timedelta(seconds=60)
    pace = _campaign_pace(
        (_wave(0, "completed", start, NOW, tasks=(("completed", 12.0),)),),
        NOW,
    )

    assert pace["current_wave"] is None
    assert pace["min_remaining_seconds"] is None
    assert pace["waves_completed"] == 1


def test_pace_without_waves_is_empty_and_never_negative() -> None:
    pace = _campaign_pace((), NOW)

    assert pace == {
        "elapsed_seconds": 0.0,
        "waves_completed": 0,
        "avg_wave_seconds": None,
        "current_wave": None,
        "last_wave_avg_task_seconds": None,
        "min_remaining_seconds": None,
    }


def test_attempt_duration_seconds_requires_both_bounds() -> None:
    started = "2026-01-01T10:00:00+00:00"
    completed = "2026-01-01T10:01:30+00:00"

    assert _attempt_duration_seconds({"started_at": started, "completed_at": completed}) == 90.0
    assert _attempt_duration_seconds({"started_at": started, "completed_at": None}) is None
    assert _attempt_duration_seconds({"completed_at": completed}) is None
    assert _attempt_duration_seconds(None) is None
    assert _attempt_duration_seconds("not-a-mapping") is None
