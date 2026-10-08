from datetime import UTC, datetime, timedelta

from merlin_agents.recon_reuse import (
    DEFAULT_RECON_REUSE_MAX_AGE,
    recon_result_is_reusable,
)


def _result(created_at: object) -> dict[str, object]:
    return {"created_at": created_at}


def test_fresh_result_is_reusable() -> None:
    now = datetime(2026, 9, 7, 12, 0, 0, tzinfo=UTC)
    created_at = now - timedelta(hours=2)
    assert recon_result_is_reusable(_result(created_at), now=now) is True


def test_stale_result_is_not_reusable() -> None:
    now = datetime(2026, 9, 7, 12, 0, 0, tzinfo=UTC)
    created_at = now - DEFAULT_RECON_REUSE_MAX_AGE - timedelta(seconds=1)
    assert recon_result_is_reusable(_result(created_at), now=now) is False


def test_exactly_at_max_age_is_reusable() -> None:
    now = datetime(2026, 9, 7, 12, 0, 0, tzinfo=UTC)
    created_at = now - DEFAULT_RECON_REUSE_MAX_AGE
    assert recon_result_is_reusable(_result(created_at), now=now) is True


def test_naive_created_at_is_treated_as_utc() -> None:
    now = datetime(2026, 9, 7, 12, 0, 0, tzinfo=UTC)
    created_at = datetime(2026, 9, 7, 10, 0, 0)  # naive, 2h earlier
    assert recon_result_is_reusable(_result(created_at), now=now) is True


def test_naive_now_is_treated_as_utc() -> None:
    now = datetime(2026, 9, 7, 12, 0, 0)  # naive now
    created_at = datetime(2026, 9, 7, 10, 0, 0, tzinfo=UTC)
    assert recon_result_is_reusable(_result(created_at), now=now) is True


def test_non_datetime_created_at_is_not_reusable() -> None:
    now = datetime(2026, 9, 7, 12, 0, 0, tzinfo=UTC)
    assert recon_result_is_reusable(_result("2026-09-07T10:00:00Z"), now=now) is False


def test_missing_created_at_is_not_reusable() -> None:
    now = datetime(2026, 9, 7, 12, 0, 0, tzinfo=UTC)
    assert recon_result_is_reusable({}, now=now) is False
