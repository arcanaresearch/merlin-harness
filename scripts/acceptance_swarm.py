"""Deterministic durability smoke plus opt-in authorized live campaign acceptance."""

from __future__ import annotations

import asyncio
import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from urllib.parse import urlparse

from merlin_agents import (
    GraphAuthority,
    GraphBounds,
    GraphError,
    ReferenceScheduler,
    SchedulerEdge,
    SchedulerEdgeKind,
    SchedulerNode,
    SchemaSpec,
    SwarmTaskSpec,
    admit_child_graph,
    admit_graph,
    build_swarm_graph,
)
from merlin_storage_postgres import (
    Database,
    DatabaseConfig,
    DurableSchedulerStore,
    MigrationRunner,
    SessionRepository,
    StaleLeaseError,
)
from merlin_tui.local_control import LocalControlClient, ensure_daemon, stop_daemon

ROOT = Path(__file__).resolve().parents[1]
LIVE_TARGET = "https://plunder.me"
LIVE_OBJECTIVE = "Run a merlin scan on https://plunder.me, policy refund trap L1"


def _max_coverage_extensions() -> int:
    """Unattended-run extension cap: env override, then config, then 3."""
    override = os.environ.get("MERLIN_LIVE_MAX_EXTENSIONS")
    if override is not None:
        return int(override)
    from merlin_config import load_config

    configured = load_config().max_coverage_extensions
    return configured if configured is not None else 3


def _plain(value: object) -> object:
    if isinstance(value, SimpleNamespace):
        return {key: _plain(item) for key, item in vars(value).items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    if isinstance(value, dict):
        return {key: _plain(item) for key, item in value.items()}
    return value


def _stream_text(events: list[object]) -> str:
    return "".join(str(getattr(event, "text", "")) for event in events)


def _tasks(status: object) -> list[dict[str, object]]:
    plain = _plain(status)
    assert isinstance(plain, dict)
    return [task for wave in plain["waves"] for task in wave["tasks"]]


def _assert_recommended_preparation_bound(status: object) -> int:
    plain = _plain(status)
    assert isinstance(plain, dict)
    saturation = plain.get("saturation")
    assert isinstance(saturation, dict), plain
    admitted = saturation.get("admitted_techniques")
    assert isinstance(admitted, int) and not isinstance(admitted, bool), saturation
    assert admitted > 0, ("Recommended preparation admitted no techniques", admitted)
    return admitted


async def _missing_suite_files(paths: list[object]) -> list[str]:
    presence = await asyncio.gather(*(asyncio.to_thread(Path(str(path)).is_file) for path in paths))
    return [str(path) for path, present in zip(paths, presence, strict=True) if not present]


async def _start_configured_release_daemon() -> None:
    release_binary = os.environ.get("MERLIN_LIVE_RELEASE_BINARY")
    if not release_binary:
        await ensure_daemon()
        return
    await stop_daemon()
    started = await asyncio.create_subprocess_exec(
        release_binary,
        "start",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await started.communicate()
    assert started.returncode == 0, (stdout.decode(), stderr.decode())
    await ensure_daemon()


async def _wait_for_terminal_campaign(
    client: LocalControlClient,
    campaign_id: str,
    *,
    timeout_seconds: float | None = None,
) -> dict[str, object]:
    if timeout_seconds is None:
        timeout_seconds = float(os.environ.get("MERLIN_LIVE_TERMINAL_TIMEOUT_SECONDS", "10800"))
    if timeout_seconds <= 0:
        raise ValueError("live campaign terminal timeout must be positive")
    status: dict[str, object] = {}
    extension_decisions = 0
    max_extensions = _max_coverage_extensions()
    handled_extension_requests: set[str] = set()
    try:
        async with asyncio.timeout(timeout_seconds):
            while True:
                status = _plain(await client.campaign_status(campaign_id))
                assert isinstance(status, dict)
                if status["status"] in {"completed", "failed", "cancelled"}:
                    return status
                decision = status.get("saturation_decision")
                if (
                    isinstance(decision, dict)
                    and decision.get("state") == "awaiting_operator"
                    and decision.get("purpose") == "coverage_extension"
                ):
                    request_id = str(decision.get("request_id", "coverage_extension"))
                    if request_id in handled_extension_requests:
                        await asyncio.sleep(1)
                        continue
                    if extension_decisions < max_extensions:
                        await client.decide_campaign_saturation(campaign_id, "continue", None)
                        extension_decisions += 1
                    else:
                        # Prove the honest terminal/report path without turning
                        # unresolved coverage into an unbounded target loop.
                        await client.decide_campaign_saturation(campaign_id, "stop", None)
                    handled_extension_requests.add(request_id)
                await asyncio.sleep(1)
    except TimeoutError:
        status = _plain(await client.campaign_status(campaign_id))
        assert isinstance(status, dict)
        cancelled = await client.cancel_campaign(campaign_id)
        raise AssertionError(
            "live campaign missed its terminal deadline and was cancelled "
            f"(cancelled={cancelled}): {json.dumps(status, sort_keys=True)}"
        ) from None


async def _assert_terminal_campaign_evidence(
    client: LocalControlClient, campaign_id: str, status: dict[str, object]
) -> None:
    """Verify terminal campaign evidence without rerunning target interactions."""

    assert status["status"] == "completed", status
    stop_reason = str(status["stop_reason"])
    # operator_stopped is honest when the operator declined further coverage
    # extensions after real work landed (see _wait_for_terminal_campaign).
    assert stop_reason in {"saturated", "completed_with_limitations", "operator_stopped"}, status
    assert status["lead_events"] >= len(status["waves"]) + 1, status
    assert status["latest_lead_stop_reason"] == status["stop_reason"], status
    terminal_tasks = _tasks(status)
    assert all(
        task["status"] in {"completed", "failed", "cancelled", "quarantined"}
        for task in terminal_tasks
    ), status
    failed_waves = [wave for wave in status["waves"] if wave["status"] == "failed"]
    if failed_waves:
        last_failed = max(int(wave["wave"]) for wave in failed_waves)
        assert any(
            int(wave["wave"]) > last_failed and wave["status"] == "completed"
            for wave in status["waves"]
        ), status

    result_text = "\n".join(str(task["result"]).lower() for task in terminal_tasks)
    # Attribution must be target-derived, not phrasing-dependent: status task
    # results are truncated previews, so word lists ("website", "chat") break
    # whenever the preview window shifts. The reviewed endpoint markers and the
    # recorded target-call evidence refs are structural.
    target_host = str(urlparse(LIVE_TARGET).hostname or "").lower()
    markers = {target_host, "policy-refund-trap", "refund"}
    assert any(marker and marker in result_text for marker in markers), (
        "workers returned no evidence attributable to the authorized target"
    )
    completion_text = json.dumps(status.get("completion_document") or {}).lower()
    evidence_markers = ("call_", "merlin-result://", "target-call", "target-interaction")
    assert any(marker in completion_text for marker in evidence_markers), (
        "terminal completion document carries no target interaction evidence"
    )

    completion = status["completion_document"]
    assert isinstance(completion, dict), status
    saturation = completion.get("saturation")
    assert isinstance(saturation, dict), status
    if stop_reason == "saturated":
        assert saturation.get("ready") is True, (
            "saturated campaign lacks conclusive required coverage",
            status,
        )
    else:
        assert saturation.get("ready") is False, (
            "campaign claimed limitations despite conclusive saturation",
            status,
        )
        assert saturation.get("execution_complete") is True, (
            "campaign claimed completion while required execution remained",
            status,
        )
    assert int(saturation.get("catalogue_cells_blocked", 0)) == 0, status
    assert int(saturation.get("hypothesis_cells_blocked", 0)) == 0, status
    if stop_reason == "saturated":
        assert int(saturation.get("catalogue_cells_unclear", 0)) == 0, status
        assert int(saturation.get("hypothesis_cells_unclear", 0)) == 0, status
        assert not saturation.get("adaptive_unresolved"), status
    receipts = completion.get("receipts")
    assert isinstance(receipts, list) and receipts, (
        "terminal campaign produced no durable execution receipts"
    )
    reports = _plain(await client.list_reports())
    campaign_reports = [item for item in reports if str(item["run"]) == campaign_id]
    assert campaign_reports, "terminal campaign produced no durable report revision"
    assert any("cleanup completed" in str(item["status"]) for item in campaign_reports), (
        "terminal report did not seal completed cleanup",
        campaign_reports,
    )
    assert all(str(item["status"]).startswith("ready") for item in campaign_reports)
    assert any(int(item.get("exchange_count", 0)) > 0 for item in campaign_reports), (
        "durable report omitted browser target exchanges"
    )


async def existing_campaign_postcheck(campaign_id: str) -> None:
    """Recheck a completed authorized campaign after a source fix or daemon restart."""

    if os.environ.get("MERLIN_LIVE_AUTHORIZED_TARGET") != LIVE_TARGET:
        raise RuntimeError(
            "live acceptance requires MERLIN_LIVE_AUTHORIZED_TARGET=https://plunder.me"
        )
    await _start_configured_release_daemon()
    client = LocalControlClient()
    status = _plain(await client.campaign_status(campaign_id))
    assert isinstance(status, dict)
    await _assert_terminal_campaign_evidence(client, campaign_id, status)


async def live_authorized_target_acceptance() -> None:
    """Run the exact operator journey against an explicitly authorized live target.

    This is deliberately strict and is never enabled by a generic truthy flag. The
    operator must name the authorized origin exactly. A missing piece of attributable
    recon, interaction, supervision, durability, or reporting evidence is a failure.
    """

    authorization = os.environ.get("MERLIN_LIVE_AUTHORIZED_TARGET")
    if authorization != LIVE_TARGET:
        raise RuntimeError(
            "live acceptance requires MERLIN_LIVE_AUTHORIZED_TARGET=https://plunder.me"
        )

    async def approve(*_arguments: object) -> str:
        return "allow_once"

    await _start_configured_release_daemon()
    client = LocalControlClient(confirmer=approve)
    observer = LocalControlClient(confirmer=approve)
    # The daemon deliberately preserves its active conversation across clients.
    # Acceptance must opt into a fresh session so a repeated objective cannot be
    # mistaken for approval of a manifest left by an earlier run.
    async for _event in client.stream("/new"):
        pass
    safe_config = json.loads(await client.export_safe_config())
    provider = safe_config.get("provider", {})
    provider_url = str(provider.get("base_url", ""))
    provider_host = (urlparse(provider_url).hostname or "").lower()
    assert provider_host not in {"", "localhost", "127.0.0.1", "::1"}, (
        "live acceptance requires a real remote provider"
    )
    assert not provider_host.endswith((".test", ".invalid")), (
        "live acceptance rejects deterministic/fake provider origins"
    )
    assert str(provider.get("model", "")) not in {"", "release-smoke"}, (
        "live acceptance requires an explicitly configured real model"
    )
    evaluation = safe_config.get("evaluation", {})
    suite_files = evaluation.get("suite_files", []) if isinstance(evaluation, dict) else []
    missing_suites = await _missing_suite_files(suite_files)
    if missing_suites:
        raise RuntimeError(
            "live acceptance requires every configured evaluation suite to survive daemon "
            f"restart; unavailable: {', '.join(missing_suites)}"
        )
    existing = {str(item["id"]) for item in _plain(await client.list_campaigns())}

    async def stream_until_campaign_created(
        prompt: str,
    ) -> tuple[list[object], list[dict[str, object]]]:
        """Stop waiting on narration once preparation is durably observable."""
        events: list[object] = []

        async def consume() -> None:
            async for event in client.stream(prompt):
                events.append(event)

        consumer = asyncio.create_task(consume())
        created: list[dict[str, object]] = []
        try:
            while not consumer.done():
                # LocalControlClient owns one serialized channel. Poll campaign
                # durability through a separate connection while the conversation
                # stream is still open; otherwise this call queues behind that stream
                # and the live acceptance deadlocks before preparation is observable.
                campaigns = _plain(await observer.list_campaigns())
                created = [item for item in campaigns if str(item["id"]) not in existing]
                if created:
                    try:
                        await asyncio.wait_for(asyncio.shield(consumer), timeout=5)
                    except TimeoutError:
                        await client.interrupt()
                        consumer.cancel()
                        await asyncio.gather(consumer, return_exceptions=True)
                    return events, created
                await asyncio.sleep(0.25)
            await consumer
            campaigns = _plain(await observer.list_campaigns())
            created = [item for item in campaigns if str(item["id"]) not in existing]
            return events, created
        finally:
            if not consumer.done():
                await client.interrupt()
                consumer.cancel()
                await asyncio.gather(consumer, return_exceptions=True)

    # The natural objective must ask for a campaign profile; internal /packs and
    # /swarm mechanics are never exposed to the operator.
    profile_prompt = [event async for event in client.stream(LIVE_OBJECTIVE)]
    profile_text = _stream_text(profile_prompt).lower()
    assert "recommended" in profile_text and "exhaustive" in profile_text, profile_text
    after_prompt = {str(item["id"]) for item in _plain(await client.list_campaigns())}
    assert after_prompt == existing, "campaign was prepared before profile selection"

    # Preparation is intentionally recon-first. A capable model may need more than
    # one provider turn to inspect the authenticated surface, prove a harmless
    # delivery, and compile the reviewable manifest. Continue the same natural
    # conversation; never bypass this with internal /packs or /swarm commands.
    recon_events, created = await stream_until_campaign_created("Recommended.")
    for _ in range(6):
        if created:
            break
        continuation, created = await stream_until_campaign_created(
            "Continue the already-authorized read-only reconnaissance and correct any prior "
            "handoff-tool error; do not wait for a callback or ask whether to continue. Prefer "
            "browser_compile_api_handoff when captured JSON proves the setup/message/cleanup "
            "lifecycle. Otherwise inspect a fresh accessibility snapshot, use exact unique "
            "role-and-name locators for the observed input and submit control, seal the DOM "
            "fallback, and then prepare the reviewable manifest."
        )
        recon_events.extend(continuation)
    assert len(created) == 1, created
    campaign = created[0]
    normalized_objective = str(campaign["objective"]).lower()
    assert "refund" in normalized_objective and any(
        level in normalized_objective for level in ("l1", "level 1")
    ), campaign
    assert campaign["profile"] == "recommended", campaign
    assert campaign["status"] == "prepared", campaign
    campaign_id = str(campaign["id"])
    prepared_status = _plain(await client.campaign_status(campaign_id))
    target_id = str(prepared_status.get("completion_document", {}).get("target_id", ""))
    assert "plunder-me" in target_id.lower(), prepared_status
    limits = prepared_status.get("budget", {}).get("limits", {})
    assert limits == {
        "turns": None,
        "tokens": None,
        "seconds": None,
        "cost_usd": None,
    }, f"natural campaign invented limits that the operator did not request: {limits}"
    admitted = _assert_recommended_preparation_bound(prepared_status)
    if os.environ.get("MERLIN_LIVE_PREPARE_ONLY") == "1":
        cancelled = await client.cancel_campaign(campaign_id)
        assert cancelled, "preparation-only acceptance could not cancel its prepared campaign"
        print(f"LIVE_RECOMMENDED_PREPARATION_OK campaign={campaign_id} cells={admitted}")
        return

    approval_events: list[object] = []

    async def consume_approval() -> None:
        async for event in client.stream(
            f"Approve and execute exactly campaign {campaign_id} with manifest digest "
            f"{campaign['manifest_digest']}."
        ):
            approval_events.append(event)

    approval = asyncio.create_task(consume_approval())
    try:
        async with asyncio.timeout(30):
            while True:
                approved_status = _plain(await client.campaign_status(campaign_id))
                if approved_status["status"] != "prepared":
                    break
                if approval.done():
                    await approval
                    raise AssertionError(
                        "exact durable approval ended without starting the campaign: "
                        + _stream_text(approval_events)
                    )
                await asyncio.sleep(0.25)
    finally:
        if not approval.done():
            approval.cancel()
            await asyncio.gather(approval, return_exceptions=True)
    assert approved_status["status"] in {"executing", "completed"}, {
        "status": approved_status,
        "transcript": _stream_text(approval_events),
    }

    # Observe actual workers and Campaign Lead state before deliberately restarting
    # the daemon. A campaign that merely compiles is not a live acceptance pass.
    async with asyncio.timeout(900):
        while True:
            status = _plain(await client.campaign_status(campaign_id))
            tasks = _tasks(status)
            if status["status"] in {"failed", "cancelled"}:
                raise AssertionError(status)
            scheduled_ids = {str(task["id"]) for wave in status["waves"] for task in wave["tasks"]}
            if (
                status["lead_events"] > 0
                and any(task["status"] in {"completed", "failed", "cancelled"} for task in tasks)
                and len(scheduled_ids) >= 2
            ):
                break
            await asyncio.sleep(1)

    initial_ids = {str(task["id"]) for task in status["waves"][0]["tasks"]}
    assert any(identifier.endswith("/recon") for identifier in initial_ids), status
    assert status["waves"][0]["kind"] == "initial", status
    assert len(scheduled_ids) >= 2, (
        "recon completed but Campaign Lead did not expand the reviewed campaign",
        status,
    )

    await _start_configured_release_daemon()
    client = LocalControlClient(confirmer=approve)
    restored = _plain(await client.campaign_status(campaign_id))
    assert restored["campaign_id"] == campaign_id
    assert restored["waves"], restored

    resume_events = []
    async for event in client.stream(f"/resume {restored['session_id']}"):
        resume_events.append(event)
    assert not any(event.kind == "error" for event in resume_events), resume_events

    status = await _wait_for_terminal_campaign(client, campaign_id)
    await _assert_terminal_campaign_evidence(client, campaign_id, status)

    # Prove the terminal campaign and report survive one final packaged-daemon
    # restart.  Do not use the public wildcard cancellation commands here: they
    # intentionally operate across daemon sessions and could terminate unrelated
    # campaigns belonging to another console.
    await _start_configured_release_daemon()
    client = LocalControlClient()
    restored_terminal = _plain(await client.campaign_status(campaign_id))
    assert isinstance(restored_terminal, dict)
    await _assert_terminal_campaign_evidence(client, campaign_id, restored_terminal)


def deterministic_postgres_smoke() -> None:
    """Exercise timing-sensitive durability without depending on provider speed."""
    with TemporaryDirectory(prefix="merlin-swarm-") as directory:
        with Database(DatabaseConfig(data_dir=Path(directory) / "postgres")) as database:
            with database.connect() as connection:
                MigrationRunner(connection).apply()
                session = SessionRepository(connection).create("durable swarm acceptance")
                store = DurableSchedulerStore(connection)
                schedule_id = store.create_schedule(
                    session.id,
                    {"kind": "specialist"},
                    {"ready": ["worker"]},
                    ["worker", "cleanup"],
                    ready_nodes=["worker"],
                )
                first = store.claim(
                    schedule_id,
                    "worker",
                    "specialist-1",
                    lease_expires_at=datetime.now(UTC) + timedelta(minutes=1),
                )
                message = store.send_message(
                    schedule_id,
                    "operator",
                    "worker",
                    "merlin.operator-guidance.v1",
                    {"message": "preserve evidence"},
                )
                connection.execute(
                    "UPDATE merlin_storage.scheduler_attempts "
                    "SET lease_expires_at = now() - interval '1 second' WHERE id = %s",
                    (first.id,),
                )
                connection.commit()
                store.expire(first.id, first.generation)
                replacement = store.claim(
                    schedule_id,
                    "worker",
                    "specialist-2",
                    lease_expires_at=datetime.now(UTC) + timedelta(minutes=1),
                )
                assert replacement.generation == first.generation + 1
                _assert_stale_commit(store, first)
                claimed = store.claim_messages(replacement.id, replacement.generation, "worker")
                assert [item.id for item in claimed] == [message.id]
                store.commit_reducer(
                    replacement.id,
                    replacement.generation,
                    schema_id="result/v1",
                    result={"content": "replacement"},
                    checkpoint={"completed": ["worker"]},
                    applied_message_ids=[message.id],
                )
                restored = DurableSchedulerStore(connection).restore(schedule_id)
                assert restored["schedule"]["checkpoint"] == {"completed": ["worker"]}
                assert restored["results"][0]["generation"] == replacement.generation
                store.verify(schedule_id)
                _assert_cancellation(store, session.id)
    _assert_bounded_child()
    _assert_deterministic_fan_in()


def _assert_stale_commit(store: DurableSchedulerStore, lease: object) -> None:
    try:
        store.commit_reducer(
            lease.id,
            lease.generation,
            schema_id="result/v1",
            result={"content": "stale"},
            checkpoint={},
        )
    except StaleLeaseError:
        return
    raise AssertionError("expired attempt committed through its stale lease")


def _assert_cancellation(store: DurableSchedulerStore, session_id: str) -> None:
    schedule_id = store.create_schedule(
        session_id, {"kind": "cancel"}, {}, ["work", "cleanup"], ready_nodes=["work"]
    )
    store.cancel_schedule(
        schedule_id,
        reason="operator",
        cleanup_node_ids=["cleanup"],
        checkpoint={"cancelled": True},
    )
    states = {row["node_id"]: row["state"] for row in store.restore(schedule_id)["nodes"]}
    assert states == {"cleanup": "ready", "work": "cancelled"}


def _graph(graph_id: str, *, depth: int) -> object:
    schemas = (SchemaSpec(id="envelope"), SchemaSpec(id="result"), SchemaSpec(id="reduced"))
    tasks = (
        SwarmTaskSpec(
            id="one",
            input_schema="result",
            result_schema="result",
            tools=frozenset({"read"}),
            grants=frozenset({"session"}),
            max_delegation_depth=depth,
        ),
        SwarmTaskSpec(id="two", input_schema="result", result_schema="result"),
    )
    return build_swarm_graph(
        graph_id, tasks, schemas, envelope_schema="envelope", reduced_schema="reduced"
    )


def _assert_bounded_child() -> None:
    parent = admit_graph(
        _graph("parent", depth=1),
        GraphAuthority(
            authority_id="session",
            tools=frozenset({"read"}),
            grants=frozenset({"session"}),
            max_delegation_depth=1,
        ),
        GraphBounds(max_delegation_depth=1),
    )
    child = admit_child_graph(
        parent,
        "worker:one",
        _graph("child", depth=0),
        GraphAuthority(
            authority_id="session",
            tools=frozenset({"read"}),
            grants=frozenset({"session"}),
            max_delegation_depth=0,
        ),
        GraphBounds(max_delegation_depth=0),
    )
    assert child.authority.max_delegation_depth == 0
    try:
        admit_child_graph(
            parent,
            "worker:one",
            _graph("wide", depth=1),
            GraphAuthority(
                authority_id="session",
                tools=frozenset({"read"}),
                grants=frozenset({"session"}),
                max_delegation_depth=1,
            ),
        )
    except GraphError:
        return
    raise AssertionError("child delegation widened its parent")


def _assert_deterministic_fan_in() -> None:
    scheduler = ReferenceScheduler(
        [SchedulerNode("b"), SchedulerNode("a"), SchedulerNode("reduce")],
        [
            SchedulerEdge("b", "reduce", SchedulerEdgeKind.FAN_IN),
            SchedulerEdge("a", "reduce", SchedulerEdgeKind.FAN_IN),
        ],
    )
    b = scheduler.claim("b", "worker-b", 0)
    a = scheduler.claim("a", "worker-a", 0)
    scheduler.complete(b, "evidence-b", 1)
    scheduler.fail(a, "blocked", 1)
    branches = json.loads(scheduler.fan_in("reduce").manifest)["branches"]
    assert [branch["node_id"] for branch in branches] == ["a", "b"]


async def main() -> None:
    deterministic_postgres_smoke()
    live = os.environ.get("MERLIN_RUN_LIVE_ACCEPTANCE") == "1"
    existing_campaign = os.environ.get("MERLIN_LIVE_EXISTING_CAMPAIGN")
    if existing_campaign:
        await existing_campaign_postcheck(existing_campaign)
    elif live:
        await live_authorized_target_acceptance()
    print(
        "LIVE_SWARM_POSTCHECK_OK"
        if existing_campaign
        else "LIVE_SWARM_ACCEPTANCE_OK"
        if live
        else "DETERMINISTIC_SWARM_ACCEPTANCE_OK"
    )
    print("specialists=distinct; graph_attempt_projections=durable")
    print("mailbox=sent; cancellation=preserved; lease_fencing=ok")
    print("child_delegation=bounded; fan_in=deterministic")
    print(
        "live_authorized_target=ok; restart=ok; browser_interaction=ok; report=ok"
        if live or existing_campaign
        else "live_authorized_target=SKIPPED (set MERLIN_RUN_LIVE_ACCEPTANCE=1 and "
        "MERLIN_LIVE_AUTHORIZED_TARGET=https://plunder.me)"
    )


if __name__ == "__main__":
    asyncio.run(main())
