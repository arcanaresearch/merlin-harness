from __future__ import annotations

import asyncio
from collections.abc import Mapping
from typing import Any

from .automation import AutomationOutcome


def outcome_payload(outcome: AutomationOutcome) -> dict[str, object]:
    return {
        "kind": "automation_outcome",
        "request_id": outcome.request_id,
        "owning_attempt_id": outcome.owning_attempt_id,
        "stage": outcome.stage,
        "state": outcome.state,
        "artifact_digest": outcome.artifact_digest,
        "run_id": None if outcome.run is None else outcome.run.run_id,
        "evidence_refs": () if outcome.run is None else outcome.run.evidence_refs,
        "error": outcome.error,
    }


async def reconcile_automation_requests(store: Any, manifest: Any, runner: Any) -> None:
    """Drain due durable requests. A fresh lease token fences every state mutation."""
    from .campaign_service import LeadAutomationRequest

    claim = getattr(store, "claim_automation_request", None)
    if not callable(claim):
        return
    while (row := await asyncio.to_thread(claim, manifest.id)) is not None:
        identity = (manifest.id, str(row["request_id"]), str(row["claim_id"]))

        async def execute(row: Mapping[str, Any] = row) -> None:
            payload = row["payload"]
            request_id = str(row["request_id"])
            owner = str(payload.get("owning_attempt_id") or "")
            identity = (manifest.id, request_id, str(row["claim_id"]))
            retryable = False
            # Always query the journal BEFORE invoking the runner. Even failed,
            # cancelled or ambiguous terminal runs must never be executed again.
            outcome = await asyncio.to_thread(
                store.automation_run_outcome, row["run_id"], request_id, owner
            )
            if outcome is None and row["state"] == "running":
                # A lease is a DB fence, not a fence around external tools. A
                # stalled former owner could still be inside the runner.
                outcome = AutomationOutcome(
                    request_id=request_id,
                    owning_attempt_id=owner,
                    stage="dispatch",
                    state="ambiguous",
                    error="automation execution lease expired; replay refused",
                )
            if outcome is None:
                if not await asyncio.to_thread(store.start_automation_request, *identity):
                    return
                try:
                    request = LeadAutomationRequest(**payload)
                    if runner is None:
                        raise OSError("automation runtime is not configured")
                    outcome = await runner(manifest, request)
                    retryable = outcome.run is None and outcome.state == "failed"
                except Exception as error:
                    outcome = AutomationOutcome(
                        request_id=request_id,
                        owning_attempt_id=owner,
                        stage="dispatch",
                        state="failed",
                        error=f"{type(error).__name__}: {error}",
                    )
                    retryable = not isinstance(error, (ValueError, TypeError, KeyError))
                # The runner can fail after persisting a run (e.g. usage
                # accounting). The journal wins over the dispatch exception.
                persisted = await asyncio.to_thread(
                    store.automation_run_outcome, row["run_id"], request_id, owner
                )
                if persisted is not None:
                    outcome, retryable = persisted, False
            await asyncio.to_thread(
                store.settle_automation_request,
                *identity,
                outcome_payload(outcome),
                retryable=retryable,
            )

        async def renew(identity: tuple[str, str, str] = identity) -> None:
            while True:
                await asyncio.sleep(15)
                if not await asyncio.to_thread(store.renew_automation_request, *identity):
                    raise RuntimeError("automation request lease lost")

        execution = asyncio.create_task(execute())
        renewal = asyncio.create_task(renew())
        try:
            done, _ = await asyncio.wait((execution, renewal), return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()
        finally:
            execution.cancel()
            renewal.cancel()
            await asyncio.gather(execution, renewal, return_exceptions=True)
