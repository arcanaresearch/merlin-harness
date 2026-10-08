#!/usr/bin/env python3
"""Campaign watchdog — watch a running campaign via the local DB. No LLM, no tokens.

Polls the daemon's Postgres through ``merlin database shell`` (plain SELECTs only)
and prints a compact liveness line every ``--interval`` seconds:

  * campaign status / stop reason and how long since it last changed
  * most recent Lead-journal event and its age
  * dynamic-specialist counts by state
  * scheduler-attempt counts (running + oldest heartbeat age)
  * budget reservations (tokens / turns / USD)

A campaign is flagged STALLED only when it is non-terminal but no semantic state
has moved (newest of campaign update, Lead journal, and non-heartbeat scheduler
event) for ``--stall-after`` seconds. Lease heartbeats are reported separately as
process liveness and never reset this progress watchdog. It never calls a provider,
so it costs nothing. In unattended acceptance, ``--fail-on-stall`` makes a
confirmed stall exit with status 3 so the owning runner can pause the campaign and
preserve its evidence.

Examples:
    scripts/campaign_watchdog.py                       # watch newest non-terminal campaign
    scripts/campaign_watchdog.py --campaign 8b869177   # watch a specific campaign (uuid or full id)
    scripts/campaign_watchdog.py --once                # single snapshot, no loop
    scripts/campaign_watchdog.py --verbose             # full detail block each tick
    scripts/campaign_watchdog.py --log target/run.log  # also show the tail of a driver log
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import time
from datetime import datetime

TERMINAL_STATUSES = {"completed", "failed", "cancelled"}
ACTIVE_STATUSES = {"prepared", "running", "paused"}

_PSQL_PREAMBLE = (
    "\\pset pager off\n"
    "\\pset format unaligned\n"
    "\\pset tuples_only on\n"
    "\\pset footer off\n"
    "\\set ON_ERROR_STOP on\n"
)

# psql echoes a confirmation line for every \pset change; drop them so only
# query rows remain.
_PSET_NOISE = {
    "Pager usage is off.",
    "Output format is unaligned.",
    "Tuples only is on.",
    "Default footer is off.",
}


def _sql_literal(value: str) -> str:
    return value.replace("'", "''")


def run_sql(sql: str, merlin: str, timeout: int = 30) -> list[str]:
    """Run SQL through ``merlin database shell`` and return non-empty output lines."""
    proc = subprocess.run(
        [merlin, "database", "shell"],
        input=_PSQL_PREAMBLE + sql + "\n",
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if proc.returncode != 0:
        err = proc.stderr.strip() or f"merlin database shell exited {proc.returncode}"
        raise RuntimeError(err)
    return [ln for ln in proc.stdout.splitlines() if ln.strip() and ln.strip() not in _PSET_NOISE]


def _campaign_row(campaign_id: str, merlin: str) -> dict[str, str] | None:
    rows = run_sql(
        "SELECT id || E'\\t' || COALESCE(status,'') || E'\\t' || COALESCE(stop_reason,'')"
        " || E'\\t' || EXTRACT(EPOCH FROM (now()-updated_at))::int"
        " || E'\\t' || COALESCE(manifest->>'specialist_composition',"
        " manifest->>'composition', manifest->>'composition_policy','')"
        " FROM merlin_storage.assessment_campaigns WHERE id = '" + _sql_literal(campaign_id) + "'",
        merlin,
    )
    if not rows:
        return None
    parts = rows[0].split("\t")
    return {
        "id": parts[0],
        "status": parts[1],
        "stop_reason": parts[2] if len(parts) > 2 else "",
        "updated_age_s": parts[3] if len(parts) > 3 else "",
        "composition": parts[4] if len(parts) > 4 else "",
    }


def _resolve_campaign(target: str | None, merlin: str) -> str:
    if target and target != "auto":
        if not target.startswith("campaign/"):
            target = "campaign/" + target
        if _campaign_row(target, merlin) is not None:
            return target
        # allow a short uuid prefix
        rows = run_sql(
            "SELECT id FROM merlin_storage.assessment_campaigns"
            " WHERE id LIKE '" + _sql_literal(target) + "%' ORDER BY updated_at DESC LIMIT 2",
            merlin,
        )
        if len(rows) == 1:
            return rows[0]
        if len(rows) > 1:
            raise SystemExit(f"ambiguous campaign prefix: {target}")
        raise SystemExit(f"campaign not found: {target}")
    rows = run_sql(
        "SELECT id FROM merlin_storage.assessment_campaigns"
        " WHERE status NOT IN ('completed','failed','cancelled')"
        " ORDER BY updated_at DESC LIMIT 1",
        merlin,
    )
    if not rows or not rows[0].strip():
        raise SystemExit("no non-terminal campaign to watch")
    return rows[0].strip()


def _last_journal(campaign_id: str, merlin: str, limit: int = 5) -> list[str]:
    return run_sql(
        "SELECT sequence || E'\\t' || COALESCE(wave::text,'') || E'\\t'"
        " || COALESCE(payload->>'kind','(none)') || E'\\t'"
        " || EXTRACT(EPOCH FROM (now()-created_at))::int || E'\\t'"
        " || COALESCE(payload->>'error', payload->>'reason','')"
        " FROM merlin_storage.campaign_lead_journal"
        " WHERE campaign_id = '" + _sql_literal(campaign_id) + "'"
        " ORDER BY sequence DESC LIMIT " + str(limit),
        merlin,
    )


def _specialists(campaign_id: str, merlin: str) -> list[str]:
    return run_sql(
        "SELECT COALESCE(state,'?') || E'\\t' || count(*)"
        " FROM merlin_storage.campaign_dynamic_specialists"
        " WHERE campaign_id = '" + _sql_literal(campaign_id) + "'"
        " GROUP BY COALESCE(state,'?') ORDER BY 1",
        merlin,
    )


def _attempts(campaign_id: str, merlin: str) -> list[str]:
    campaign = _sql_literal(campaign_id)
    return run_sql(
        "WITH campaign_schedules AS ("
        " SELECT schedule_id FROM merlin_storage.assessment_campaigns"
        " WHERE id = '" + campaign + "'"
        " UNION"
        " SELECT schedule_id FROM merlin_storage.campaign_waves"
        " WHERE campaign_id = '" + campaign + "'"
        ")"
        " SELECT COALESCE(a.state,'?') || E'\\t' || count(*) || E'\\t'"
        " || COALESCE(EXTRACT(EPOCH FROM (now()-MAX(a.heartbeat_at)))::int::text,'')"
        " FROM merlin_storage.scheduler_attempts a"
        " JOIN campaign_schedules s ON s.schedule_id = a.schedule_id"
        " GROUP BY COALESCE(a.state,'?') ORDER BY 1",
        merlin,
    )


def _scheduler_activity_age(campaign_id: str, merlin: str) -> str | None:
    """Return the age of the newest scheduler event that represents work progress.

    Lease heartbeats prove only that the worker process still owns its lease.  They
    deliberately do not count as progress: a provider request can wait forever
    while the independent lease keeper continues to emit fresh heartbeats.
    """
    campaign = _sql_literal(campaign_id)
    rows = run_sql(
        "WITH campaign_schedules AS ("
        " SELECT schedule_id FROM merlin_storage.assessment_campaigns"
        " WHERE id = '" + campaign + "'"
        " UNION"
        " SELECT schedule_id FROM merlin_storage.campaign_waves"
        " WHERE campaign_id = '" + campaign + "'"
        ")"
        " SELECT COALESCE("
        " EXTRACT(EPOCH FROM (now()-MAX(e.created_at)))::int::text,'')"
        " FROM merlin_storage.scheduler_events e"
        " JOIN campaign_schedules s ON s.schedule_id = e.schedule_id"
        " WHERE e.type <> 'heartbeat'",
        merlin,
    )
    if not rows or not rows[0].strip():
        return None
    return rows[0].strip()


def _budget(campaign_id: str, merlin: str) -> list[str]:
    return run_sql(
        "SELECT COALESCE(SUM(reserved_tokens),0) || E'\\t' || COALESCE(SUM(settled_tokens),0)"
        " || E'\\t' || COALESCE(SUM(reserved_turns),0) || E'\\t' || COALESCE(SUM(settled_turns),0)"
        " || E'\\t' || COALESCE(SUM(reserved_cost_usd),0) || E'\\t'"
        " || COALESCE(SUM(settled_cost_usd),0)"
        " FROM merlin_storage.campaign_budget_reservations"
        " WHERE campaign_id = '" + _sql_literal(campaign_id) + "'",
        merlin,
    )


def _log_tail(log_path: str, lines: int = 3) -> list[str]:
    try:
        with open(log_path, encoding="utf-8", errors="replace") as fh:
            return [ln.rstrip() for ln in fh.readlines()[-lines:] if ln.strip()]
    except OSError:
        return []


def _now() -> str:
    return datetime.now().strftime("%H:%M:%S")


def _fmt_ts_ago(age_s: str) -> str:
    try:
        s = int(age_s)
    except (TypeError, ValueError):
        return "?"
    if s < 0:
        return "0s"
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m{s % 60}s"
    return f"{s // 3600}h{(s % 3600) // 60}m"


def snapshot(campaign_id: str, merlin: str, log_path: str | None) -> None:
    camp = _campaign_row(campaign_id, merlin)
    if camp is None:
        print(f"[{_now()}] campaign disappeared: {campaign_id}")
        return

    status = camp["status"]
    comp = f" comp={camp['composition']}" if camp["composition"] else ""
    print(
        f"[{_now()}] {campaign_id} status={status}"
        f"{' stop=' + camp['stop_reason'] if camp['stop_reason'] else ''}"
        f" (changed {_fmt_ts_ago(camp['updated_age_s'])} ago){comp}"
    )

    journal = _last_journal(campaign_id, merlin)
    if journal:
        top = journal[0].split("\t")
        # seq, wave, kind, age_s, detail
        last_kind = top[2]
        last_age = top[3]
        wave = top[1]
        detail = top[4] if len(top) > 4 else ""
        line = f"  lead: wave={wave or '-'} last={last_kind} {_fmt_ts_ago(last_age)} ago"
        if detail:
            line += f"  [{detail[:120]}]"
        print(line)
        for row in journal[1:3]:
            p = row.split("\t")
            extra = f" [{p[4][:120]}]" if len(p) > 4 and p[4] else ""
            print(f"        prev: {p[2]} {_fmt_ts_ago(p[3])} ago{extra}")

    specs = _specialists(campaign_id, merlin)
    if specs:
        print(
            "  specialists: "
            + ", ".join(f"{s.split(chr(9))[0]}={s.split(chr(9))[1]}" for s in specs)
        )

    attempts = _attempts(campaign_id, merlin)
    if attempts:
        parts = []
        for a in attempts:
            p = a.split("\t")
            st, n, hb = p[0], p[1], p[2] if len(p) > 2 else ""
            suffix = f" (oldest hb {_fmt_ts_ago(hb)} ago)" if hb else ""
            parts.append(f"{st}={n}{suffix}")
        print("  attempts: " + ", ".join(parts))
    else:
        print("  attempts: none")

    budget = _budget(campaign_id, merlin)
    if budget:
        b = budget[0].split("\t")
        # reserved_tokens, settled_tokens, reserved_turns, settled_turns, reserved_usd, settled_usd
        print(
            f"  budget: reserved {b[0]}tok/{b[2]}turns/{b[4]}usd"
            f" · settled {b[1]}tok/{b[3]}turns/{b[5]}usd"
        )

    if log_path:
        tail = _log_tail(log_path)
        if tail:
            print("  log tail:")
            for ln in tail:
                print(f"    {ln[-160:]}")


def stall_state(campaign_id: str, merlin: str, stall_after: int) -> tuple[bool, str]:
    """Return (stalled, newest_activity_ago_s) for a non-terminal campaign."""
    camp = _campaign_row(campaign_id, merlin)
    if camp is None:
        return False, "0"
    if camp["status"] in TERMINAL_STATUSES:
        return False, "0"

    # newest activity = smallest age across the three signals
    ages: list[int] = []
    try:
        ages.append(int(camp["updated_age_s"] or "0"))
    except ValueError:
        pass

    for row in _last_journal(campaign_id, merlin, limit=1):
        try:
            ages.append(int(row.split("\t")[3]))
        except (IndexError, ValueError):
            pass

    scheduler_age = _scheduler_activity_age(campaign_id, merlin)
    if scheduler_age is not None:
        try:
            ages.append(int(scheduler_age))
        except ValueError:
            pass

    if not ages:
        return True, "?"

    newest_age = min(ages)  # seconds since newest activity
    stalled = newest_age > stall_after
    return stalled, str(max(newest_age, 0))


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument(
        "--campaign", default="auto", help="campaign id or 'auto' (default: newest non-terminal)"
    )
    ap.add_argument("--interval", type=int, default=15, help="seconds between polls (default 15)")
    ap.add_argument(
        "--stall-after",
        type=int,
        default=300,
        help="flag STALLED after N seconds of no activity (default 300)",
    )
    ap.add_argument(
        "--fail-on-stall",
        action="store_true",
        help="exit 3 after a confirmed stall instead of monitoring indefinitely",
    )
    ap.add_argument(
        "--stall-confirmations",
        type=int,
        default=2,
        help="consecutive stalled polls required by --fail-on-stall (default 2)",
    )
    ap.add_argument("--once", action="store_true", help="print one snapshot and exit")
    ap.add_argument("--verbose", action="store_true", help="always print the full detail block")
    ap.add_argument("--log", help="also show the tail of this log file each tick")
    ap.add_argument(
        "--merlin", default="./merlin", help="path to the merlin launcher (default ./merlin)"
    )
    args = ap.parse_args()
    if args.interval <= 0 or args.stall_after <= 0 or args.stall_confirmations <= 0:
        ap.error("interval, stall-after, and stall-confirmations must be positive")

    # Stream each tick immediately even when stdout is a file or pipe.
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except (AttributeError, OSError):
        pass

    campaign_id = _resolve_campaign(args.campaign, args.merlin)
    print(
        f"watching {campaign_id} (interval={args.interval}s, "
        f"stall-after={args.stall_after}s, Ctrl+C to stop)"
    )
    stalled_polls = 0

    while True:
        try:
            # In auto mode re-resolve each tick so the watch follows the newest
            # non-terminal campaign as new ones are created.
            if args.campaign == "auto":
                try:
                    campaign_id = _resolve_campaign("auto", args.merlin)
                except SystemExit:
                    pass  # no non-terminal campaign yet; keep the last known id

            camp = _campaign_row(campaign_id, args.merlin)
            if camp and camp["status"] in TERMINAL_STATUSES:
                snapshot(campaign_id, args.merlin, args.log)
                print(
                    f"[{_now()}] TERMINAL: status={camp['status']}"
                    f"{' stop_reason=' + camp['stop_reason'] if camp['stop_reason'] else ''}"
                )
                return 0

            stalled, newest_activity_age = stall_state(campaign_id, args.merlin, args.stall_after)
            if args.verbose:
                snapshot(campaign_id, args.merlin, args.log)
            else:
                # compact single-line liveness
                camp = _campaign_row(campaign_id, args.merlin) or {}
                journal = _last_journal(campaign_id, args.merlin, limit=1)
                last = journal[0].split("\t") if journal else []
                specs = _specialists(campaign_id, args.merlin)
                attempts = _attempts(campaign_id, args.merlin)
                last_kind = last[2] if len(last) > 2 else "-"
                last_age = last[3] if len(last) > 3 else "?"
                lead_txt = (
                    f"{last_kind} {_fmt_ts_ago(last_age)} ago" if journal else "no journal yet"
                )
                running = sum(
                    int(a.split("\t")[1])
                    for a in attempts
                    if a.split("\t")[0] in {"running", "active"}
                )
                live_heartbeat_ages = [
                    int(parts[2])
                    for row in attempts
                    if (parts := row.split("\t"))
                    and parts[0] in {"running", "active"}
                    and len(parts) > 2
                    and parts[2].isdigit()
                ]
                lease_liveness = (
                    f"lease_hb={_fmt_ts_ago(min(live_heartbeat_ages))} ago"
                    if live_heartbeat_ages
                    else "lease_hb=none"
                )
                n_specs = sum(int(s.split("\t")[1]) for s in specs)
                flag = "STALLED" if stalled else "ok"
                print(
                    f"[{_now()}] {campaign_id} status={camp.get('status', '?')}"
                    f" lead={lead_txt}"
                    f" | specs={n_specs} attempts_running={running} {lease_liveness} | {flag}"
                )

            stalled_polls = stalled_polls + 1 if stalled else 0
            if args.fail_on_stall and stalled_polls >= args.stall_confirmations:
                if not args.verbose:
                    snapshot(campaign_id, args.merlin, args.log)
                print(
                    f"[{_now()}] CONFIRMED_STALL: no activity for "
                    f"{_fmt_ts_ago(newest_activity_age)}; "
                    f"observed in {stalled_polls} consecutive polls"
                )
                return 3

            if args.once:
                return 0
            time.sleep(args.interval)
        except KeyboardInterrupt:
            print(f"\n[{_now()}] stopped by user")
            return 130
        except RuntimeError as exc:
            print(f"[{_now()}] ERROR: {exc}", file=sys.stderr)
            return 2


if __name__ == "__main__":
    raise SystemExit(main())
