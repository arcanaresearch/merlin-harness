"""Drift-aware reconnaissance reuse policy.

Reconnaissance is a live probe of the reviewed target, so a prior result is
reusable only while it is fresh enough that the reviewed scenario is unlikely
to have drifted. This module keeps that decision a pure function so the
staleness rule is testable without a database or a live target.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime, timedelta

DEFAULT_RECON_REUSE_MAX_AGE = timedelta(hours=6)


def recon_result_is_reusable(
    result: Mapping[str, object],
    *,
    now: datetime,
    max_age: timedelta = DEFAULT_RECON_REUSE_MAX_AGE,
) -> bool:
    """Return True when a persisted recon result is fresh enough to reuse.

    The timestamp is treated as UTC when it is naive. This is a time-based
    staleness bound, not a full liveness re-probe: drift within ``max_age`` is
    possible but bounded by the operator's review cadence.
    """
    created_at = result.get("created_at")
    if not isinstance(created_at, datetime):
        return False
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=UTC)
    if now.tzinfo is None:
        now = now.replace(tzinfo=UTC)
    return now - created_at <= max_age
