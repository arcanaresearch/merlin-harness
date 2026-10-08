"""Offline review-only disposition pass for stranded unvalidated finding drafts.

Builds per-finding evidence bundles from committed scheduler evidence (the
creator task's result payload plus its acknowledged tool-effect exchanges),
asks the cheapest configured validator-role model for typed verdicts in
batches, commits each campaign's verdict set as a durable scheduler result
row, and applies outcomes through FindingService with the same
merlin-result:// review witness rules as the TUI reconciler. No target
contact is made.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import sys
import tomllib
from datetime import UTC
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import httpx
import psycopg
from merlin_agents.campaign_service import _validator_evidence_bundles
from merlin_contracts import EvidenceWitness
from merlin_findings import FindingService
from merlin_packs import reviewed_execution_pairs
from merlin_storage_postgres import DurableSchedulerStore, PostgresFindingStore
from psycopg.rows import dict_row

DATABASE_URL = os.environ.get(
    "MERLIN_DATABASE_URL", "postgresql://postgres:postgres@127.0.0.1:5433/postgres"
)
CONFIG_PATH = Path.home() / ".merlin" / "config.toml"
CREDENTIALS_PATH = Path.home() / ".merlin" / "credentials.json"

SESSIONS = {
    "fc7d1571-3751-4f03-9b8a-54ba920367d6": "09ff0c4b",
    "d7f6f1b1-e679-4323-a7fa-203033ede956": "b6c90d70",
    "bd9e4b30-3afc-4213-8ade-815fb88187e3": "35bfda27",
    "0b788e06-1e25-4300-b28b-d085d7a863c3": "dbac4553",
}
BATCH_SIZE = 8
HIGH_SEVERITIES = {"high", "critical"}

SYSTEM_PROMPT = (
    "You are an independent security-finding validator in review mode. You must not "
    "contact or mutate the target; judge each candidate only from its committed, "
    "attributable evidence bundle. For every candidate finding return a typed verdict: "
    "validated only when the committed target exchanges prove the claimed security "
    "impact; refuted when the committed exchanges contradict the claim; inconclusive "
    "when the evidence is missing, truncated, ambiguous, or shows only benign behavior. "
    "When in doubt, answer inconclusive. witness_refs must list the committed evidence "
    "references or target call ids your verdict rests on. Answer with one JSON object "
    '{"validation_results": [{"finding_id": str, "outcome": '
    '"validated"|"refuted"|"inconclusive", "summary": str, "witness_refs": [str]}]} '
    "covering every candidate exactly once."
)


def _required_pair(technique_id: str) -> str | None:
    base_id = technique_id.split("@", 1)[0].strip()
    if not base_id:
        return None
    pairs, unsupported = reviewed_execution_pairs()
    if base_id in unsupported:
        return None
    return pairs.get(base_id)


def _creator_evidence(
    connection: psycopg.Connection, creator: str
) -> tuple[str, dict[str, object]]:
    """Resolve a finding creator to its committed result row and effect documents."""
    task_id = creator.removeprefix("worker:")
    rows = connection.execute(
        """SELECT schedule_id, node_id, attempt_id, payload
           FROM merlin_storage.scheduler_results
           WHERE node_id IN (%s, %s) AND schema_id = 'schema/result'
           ORDER BY created_at DESC""",
        (creator, f"worker:{task_id}"),
    ).fetchall()
    if not rows:
        # Creators with a sub-suffix (e.g. /observation-...) belong to the
        # longest matching committed worker node.
        rows = connection.execute(
            """SELECT schedule_id, node_id, attempt_id, payload
               FROM merlin_storage.scheduler_results
               WHERE %s LIKE node_id || '/%%' AND schema_id = 'schema/result'
               ORDER BY length(node_id) DESC, created_at DESC""",
            (creator,),
        ).fetchall()
    documents: dict[str, object] = {}
    payload: dict[str, object] = {}
    if rows:
        row = rows[0]
        payload = row["payload"] if isinstance(row["payload"], dict) else {}
        effects = connection.execute(
            """SELECT call_id, tool_name, arguments_payload, result_content
               FROM merlin_storage.tool_effects
               WHERE attempt_id = %s AND state IN ('acknowledged', 'reconciled')
                 AND result_is_error IS NOT TRUE AND result_content IS NOT NULL
               ORDER BY created_at""",
            (str(row["attempt_id"]),),
        ).fetchall()
        for effect in effects:
            documents[str(effect["call_id"])] = {
                "tool": effect["tool_name"],
                "arguments": effect["arguments_payload"],
                "result": effect["result_content"],
            }
    evidence: dict[str, object] = {"target_call_documents": documents}
    content = payload.get("content")
    if isinstance(content, str) and content.strip():
        evidence["worker_result_prose"] = content
    return task_id, evidence


def _verify_review_witness(
    scheduler: DurableSchedulerStore,
    witness: EvidenceWitness,
    expected_session_id: str,
    expected_target_id: str,
) -> bool:
    """Mirror of the TUI's merlin-result review-witness verification."""
    del expected_target_id  # review witnesses attest the validator row, not effects
    try:
        parsed = urlsplit(witness.reference)
        parts = [part for part in parsed.path.split("/") if part]
        if parsed.scheme != "merlin-result" or parsed.netloc != "schedule":
            return False
        if len(parts) != 3 or parts[1] != "attempt":
            return False
        schedule_id, attempt_id = parts[0], parts[2]
        values = parse_qs(parsed.query)
        kinds = values.get("witness_kind", ())
        if (
            set(values) - {"digest", "witness_kind"}
            or values.get("digest") != [witness.digest]
            or len(kinds) > 1
            or (kinds and kinds[0] not in {"review", "evidence_validation"})
        ):
            return False
        if witness.source != (
            f"independent-validator:{schedule_id}:{attempt_id}"
            f":{witness.digest.removeprefix('sha256:')}"
        ):
            return False
        schedule = scheduler.load_schedule(schedule_id)
        if not isinstance(schedule, dict) or str(schedule.get("session_id")) != (
            expected_session_id
        ):
            return False
        row = next(
            (
                item
                for item in scheduler.list_results(schedule_id)
                if str(item.get("attempt_id")) == attempt_id
            ),
            None,
        )
        if row is None:
            return False
        return bool(
            scheduler.verify_committed_evidence(
                SimpleNamespace(
                    schedule_id=schedule_id,
                    node_id=str(row.get("node_id")),
                    attempt_id=attempt_id,
                    result_digest=witness.digest,
                )
            )
        )
    except (AttributeError, KeyError, TypeError, ValueError):
        return False


def _salvage_results(text: str) -> list[dict[str, object]]:
    """Extract complete validation_result objects from truncated validator JSON."""
    decoder = json.JSONDecoder()
    results: list[dict[str, object]] = []
    for match in re.finditer(r"\{", text):
        try:
            value, _ = decoder.raw_decode(text, match.start())
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict) and "finding_id" in value and "outcome" in value:
            results.append(value)
    return results


async def _review_batch(
    client: httpx.AsyncClient,
    config: dict[str, str],
    bundles: dict[str, str],
) -> tuple[list[dict[str, object]], dict[str, int]]:
    evidence = "\n\n".join(bundles.values())
    print(f"  review batch: {len(bundles)} findings, {len(evidence)} chars", flush=True)
    response = await client.post(
        f"{config['base_url']}/chat/completions",
        headers={"Authorization": f"Bearer {config['api_key']}"},
        json={
            "model": config["model"],
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": evidence},
            ],
            "response_format": {"type": "json_object"},
            "temperature": 0.0,
            "max_tokens": 16384,
        },
        timeout=httpx.Timeout(300.0, connect=30.0),
    )
    response.raise_for_status()
    body = response.json()
    usage = body.get("usage") or {}
    message = body["choices"][0]["message"]["content"]
    if not isinstance(message, str) or not message.strip():
        raise ValueError("validator returned empty content (reasoning budget exhausted)")
    text = message.strip()
    if text.startswith("```"):
        text = text.split("```", 2)[1]
        if text.startswith("json"):
            text = text[4:]
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        # Tolerate truncated output by keeping complete result objects only.
        parsed = {"validation_results": _salvage_results(text)}
    results = parsed.get("validation_results")
    if not isinstance(results, list):
        raise ValueError("validator response lacks validation_results")
    return results, {
        "input": int(usage.get("prompt_tokens", 0)),
        "output": int(usage.get("completion_tokens", 0)),
    }


async def main() -> int:
    config = tomllib.loads(CONFIG_PATH.read_text())
    provider = config["provider"]
    role_models = provider.get("role_models", {})
    llm = {
        "base_url": provider["base_url"].rstrip("/"),
        "api_key": json.loads(CREDENTIALS_PATH.read_text())["api_key"],
        "model": role_models.get("validator") or provider["model"],
    }
    connection = psycopg.connect(DATABASE_URL, row_factory=dict_row, autocommit=True)
    scheduler = DurableSchedulerStore(connection)
    store = PostgresFindingStore(connection)
    findings_service = FindingService(
        store,
        workspace_root=Path.cwd(),
        validator_evidence_verifier=lambda witness, session_id, target_id: _verify_review_witness(
            scheduler, witness, session_id, target_id
        ),
        comparison_receipt_requirement=lambda finding: _required_pair(finding.technique_id),
    )

    totals = {"input": 0, "output": 0}
    report: dict[str, dict[str, int]] = {}
    async with httpx.AsyncClient() as client:
        for session_id, campaign in SESSIONS.items():
            all_findings = await findings_service.list_findings_for_completion(session_id)
            candidates = [
                finding for finding in all_findings if finding.validation == "unvalidated"
            ]
            counts = {"validated": 0, "refuted": 0, "inconclusive": 0, "skipped": 0}
            report[campaign] = counts
            if not candidates:
                continue

            print(f"[{campaign}] {len(candidates)} unvalidated candidates", flush=True)
            source_evidence: dict[str, dict[str, object]] = {}
            for finding in candidates:
                task_id, evidence = _creator_evidence(connection, finding.creator)
                source_evidence.setdefault(task_id, evidence)
            print(
                f"[{campaign}] resolved evidence for {len(source_evidence)} creator tasks",
                flush=True,
            )
            verdicts: dict[str, dict[str, object]] = {}
            for start in range(0, len(candidates), BATCH_SIZE):
                batch = candidates[start : start + BATCH_SIZE]
                bundles = _validator_evidence_bundles(
                    list(batch),
                    tuple(finding.id for finding in batch),
                    source_evidence,
                    max_documents=8,
                    document_char_limit=1200,
                )
                try:
                    results, usage = await _review_batch(client, llm, bundles)
                except Exception as error:  # noqa: BLE001 - batch failure must not strand the rest
                    print(f"[{campaign}] batch starting {start} failed: {error}", flush=True)
                    continue
                totals["input"] += usage["input"]
                totals["output"] += usage["output"]
                raw_counts: dict[str, int] = {}
                for result in results:
                    if isinstance(result, dict) and isinstance(result.get("finding_id"), str):
                        verdicts[result["finding_id"]] = result
                        raw = str(result.get("outcome"))
                        raw_counts[raw] = raw_counts.get(raw, 0) + 1
                print(f"  raw verdicts: {raw_counts}", flush=True)

            valid = [
                result
                for result in verdicts.values()
                if result.get("outcome") in {"validated", "refuted", "inconclusive"}
                and isinstance(result.get("summary"), str)
                and result["summary"].strip()
                and isinstance(result.get("witness_refs"), list)
            ]
            witness: EvidenceWitness | None = None
            if valid:
                # Node ids must embed the full campaign id (e.g.
                # campaign/<uuid>/backfill/...) so report-time campaign binding
                # can tie the validator result back to the owning campaign.
                full_campaign_id = next(
                    (
                        finding.creator.split("/wave-", 1)[0]
                        for finding in candidates
                        if finding.creator.startswith("campaign/") and "/wave-" in finding.creator
                    ),
                    f"campaign/{campaign}",
                )
                node_id = f"{full_campaign_id}/backfill/independent-validator"
                schedule_id = scheduler.create_schedule(
                    session_id,
                    {"kind": "validator-backfill-review", "assignments": []},
                    {"kind": "offline-review"},
                    [node_id],
                    ready_nodes=[node_id],
                )
                lease = scheduler.claim(
                    schedule_id,
                    node_id,
                    owner="scripts/disposition_stranded_drafts.py",
                    lease_expires_at=_lease_expiry(),
                )
                digest = scheduler.commit_reducer(
                    lease.id,
                    lease.generation,
                    schema_id="schema/result",
                    result={
                        "content": "Offline review-only disposition of stranded drafts.",
                        "structured": {"validation_results": valid},
                    },
                    checkpoint={"kind": "offline-review"},
                )
                witness = EvidenceWitness(
                    reference=(
                        f"merlin-result://schedule/{schedule_id}/attempt/{lease.id}"
                        f"?digest=sha256:{digest}&witness_kind=review"
                    ),
                    digest=f"sha256:{digest}",
                    source=f"independent-validator:{schedule_id}:{lease.id}:{digest}",
                )

            for finding in candidates:
                result = verdicts.get(finding.id)
                if result is None or witness is None:
                    counts["skipped"] += 1
                    continue
                outcome = str(result.get("outcome"))
                if outcome == "blocked":
                    outcome = "inconclusive"
                summary = str(result.get("summary", "")).strip()
                refs = [
                    str(ref)
                    for ref in result.get("witness_refs", [])
                    if isinstance(ref, str) and ref.strip()
                ]
                if refs:
                    summary = f"{summary} Evidence: {', '.join(refs[:6])}."
                reproduction_required = bool(
                    finding.requires_independent_reproduction
                    or finding.impact_level >= 3
                    or finding.severity in HIGH_SEVERITIES
                )
                if outcome in {"validated", "refuted"} and reproduction_required:
                    outcome = "inconclusive"
                    summary += (
                        " Review-only pass: severity/impact requires independent target "
                        "reproduction, which an offline review cannot durably attest."
                    )
                if outcome in {"validated", "refuted"} and _required_pair(finding.technique_id):
                    outcome = "inconclusive"
                    summary += (
                        " Review-only pass: paired treatment finding requires the sealed "
                        "treatment/control comparison receipt, unavailable offline."
                    )
                try:
                    if outcome == "validated":
                        await findings_service.validate_finding(
                            session_id, finding.id, summary=summary, witnesses=(witness,)
                        )
                    elif outcome == "refuted":
                        await findings_service.refute_finding(
                            session_id, finding.id, summary=summary, witnesses=(witness,)
                        )
                    else:
                        await findings_service.mark_inconclusive(
                            session_id, finding.id, summary=summary, witnesses=(witness,)
                        )
                    counts[outcome if outcome in counts else "inconclusive"] += 1
                except ValueError as error:
                    if outcome == "inconclusive":
                        print(f"[{campaign}] finding {finding.id} rejected: {error}")
                        counts["skipped"] += 1
                        continue
                    try:
                        await findings_service.mark_inconclusive(
                            session_id,
                            finding.id,
                            summary=(
                                f"{summary} Validator verdict '{outcome}' was rejected by "
                                f"the finding gates offline ({error}); preserving an "
                                "inconclusive result."
                            ),
                            witnesses=(witness,),
                        )
                        counts["inconclusive"] += 1
                    except ValueError as fallback_error:
                        print(f"[{campaign}] finding {finding.id} rejected: {fallback_error}")
                        counts["skipped"] += 1
            print(f"[{campaign}] {counts}")

    print(f"tokens: input={totals['input']} output={totals['output']}")
    print(json.dumps(report, indent=2))
    return 0


def _lease_expiry():
    from datetime import datetime, timedelta

    return datetime.now(UTC) + timedelta(minutes=30)


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
