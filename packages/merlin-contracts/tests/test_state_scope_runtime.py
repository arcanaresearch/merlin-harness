from __future__ import annotations

from typing import Any

import pytest
from merlin_contracts.state_scope_runtime import StateScopeLifecycle


class _Store:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []

    def pause_state_scope(self, *args: Any, **kwargs: Any) -> None:
        self.calls.append(("pause", args, kwargs))

    def settle_state_scope(self, *args: Any, **kwargs: Any) -> None:
        self.calls.append(("settle", args, kwargs))


def _lifecycle(store: _Store, *, acknowledged: bool) -> StateScopeLifecycle:
    lifecycle = StateScopeLifecycle(
        store=store,  # type: ignore[arg-type]
        campaign_id="campaign/test",
        target_authority="target.test",
        scope_id="state-scope:test",
        attempt_id="attempt/test",
        experiment_id="attempt/test",
        action_id="action/test",
        candidate_id="candidate/test",
        acknowledged=acknowledged,
    )
    lifecycle.bind_lease("claim/test", "claimant/test")
    return lifecycle


@pytest.mark.asyncio
async def test_pause_before_delivery_uses_pre_delivery_transition() -> None:
    store = _Store()
    lifecycle = _lifecycle(store, acknowledged=False)

    await lifecycle.pause(reason="transport_failed")

    assert [call[0] for call in store.calls] == ["pause"]
    assert store.calls[0][2]["transition_disposition"] == "infrastructure_failure"
    assert lifecycle.finished is True


@pytest.mark.asyncio
async def test_pause_after_delivery_settles_without_replaying_effect() -> None:
    store = _Store()
    lifecycle = _lifecycle(store, acknowledged=True)

    await lifecycle.pause(
        action_refs=("evidence:action",),
        reason="capture_incomplete",
        retryable=True,
    )

    assert [call[0] for call in store.calls] == ["settle"]
    _, args, kwargs = store.calls[0]
    envelope = args[2]
    assert envelope["action"]["evidence_refs"] == ["evidence:action"]
    assert envelope["responses"]["missing"] == {
        "reason": "capture_incomplete",
        "retryable": True,
    }
    assert kwargs["transition_disposition"] == "infrastructure_failure"
    assert lifecycle.finished is True
