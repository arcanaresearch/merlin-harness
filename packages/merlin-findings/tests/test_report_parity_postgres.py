import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Barrier

import pytest
from merlin_findings import DurableReportService
from merlin_storage_postgres import (
    CampaignStore,
    Database,
    DatabaseConfig,
    MigrationRunner,
    PostgresReportRevisionStore,
    SessionRepository,
)


def _digest(value: object) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _seal(campaign_id: str) -> tuple[dict, str]:
    document = {
        "schema": "merlin.campaign-completion.v1",
        "campaign_id": campaign_id,
        "manifest_digest": "sha256:" + "a" * 64,
        "target_id": "target-1",
        "profile": "recommended",
        "cells": (),
        "waves": (),
        "receipts": (),
        "findings": (),
        "cleanup": {"expected": (), "states": (), "authority": {}},
        "saturation": {"ready": False},
    }
    receipt = {
        "kind": "campaign_completion",
        "schema": "merlin.campaign-completion.v1",
        "completion_digest": _digest(document),
        "terminal_status": "completed",
        "stop_reason": "capped",
        "document": document,
    }
    return receipt, _digest(receipt)


def test_sealed_and_legacy_reports_restart_idempotently_and_fail_on_index_drift(
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "postgres"
    campaign_id = "campaign/report-parity"
    created_at = datetime(2030, 1, 1, tzinfo=UTC)
    receipt, receipt_digest = _seal(campaign_id)
    with Database(DatabaseConfig(data_dir=data_dir)) as database:
        with database.connect() as connection:
            MigrationRunner(connection).apply()
            session = SessionRepository(connection).create("report parity")
            CampaignStore(connection).prepare(
                campaign_id,
                str(session.id),
                {"manifest": {"objective": "report parity"}, "assignment_context": {}},
                "sha256:" + "a" * 64,
            )
            reports = DurableReportService(PostgresReportRevisionStore(connection))
            kwargs = {
                "stop_reason": "capped",
                "cleanup_status": "completed",
                "findings": (),
                "tasks": (),
                "evidence": (),
                "usage": {},
                "metadata": {
                    "manifest_digest": "sha256:" + "a" * 64,
                    "profile": "recommended",
                },
                "completion_document": receipt,
                "completion_digest": receipt_digest,
                "created_at": created_at,
            }
            first = reports.create(campaign_id, **kwargs)
            repeated = reports.create(campaign_id, **kwargs)
            assert repeated.revision_id == first.revision_id

            legacy_source = {
                "campaign_id": "campaign/legacy-report",
                "revision": 1,
                "stop_reason": "interrupted",
                "cleanup_status": "pending",
                "findings": [],
                "tasks": [],
                "evidence": [],
                "usage": {},
                "created_at": created_at.isoformat(),
                "audience": "internal",
                "report_state": "incomplete",
                "metadata": {},
                "coverage": {},
                "techniques": [],
                "exchanges": [],
                "lineage": [],
                "unsearched_frontier": [],
                "limitations": [],
            }
            legacy_digest = _digest(legacy_source)
            legacy_revision_id = "campaign/legacy-report:r1:" + legacy_digest[-12:]
            legacy_projection = {
                **legacy_source,
                "revision_id": legacy_revision_id,
                "digest": legacy_digest,
            }
            connection.execute(
                """INSERT INTO merlin_storage.report_revisions
                   (revision_id, campaign_id, revision, digest, projection, created_at)
                   VALUES (%s, %s, 1, %s, %s::jsonb, %s)""",
                (
                    legacy_revision_id,
                    "campaign/legacy-report",
                    legacy_digest,
                    json.dumps(legacy_projection),
                    created_at,
                ),
            )

            # The latest durable report query is unbounded and publication-ordered.
            for index in range(101):
                extra_campaign = f"campaign/history-{index:03d}"
                CampaignStore(connection).prepare(
                    extra_campaign,
                    str(session.id),
                    {"manifest": {"objective": "history"}, "assignment_context": {}},
                    f"sha256:{index:064x}",
                )
                DurableReportService(PostgresReportRevisionStore(connection)).create(
                    extra_campaign,
                    stop_reason="interrupted",
                    cleanup_status="pending",
                    findings=(),
                    tasks=(),
                    evidence=(),
                    usage={},
                    created_at=created_at + timedelta(seconds=index + 1),
                )
            latest = PostgresReportRevisionStore(connection).latest_for_session(str(session.id))
            assert latest is not None and latest.campaign_id == "campaign/history-100"
            assert len(CampaignStore(connection).list_all(limit=None)) >= 102

        concurrent_campaign = "campaign/concurrent-report"
        concurrent_receipt, concurrent_digest = _seal(concurrent_campaign)
        barrier = Barrier(2)

        def publish() -> str:
            with database.connect() as thread_connection:
                service = DurableReportService(PostgresReportRevisionStore(thread_connection))
                barrier.wait(timeout=5)
                report = service.create(
                    concurrent_campaign,
                    stop_reason="capped",
                    cleanup_status="completed",
                    findings=(),
                    tasks=(),
                    evidence=(),
                    usage={},
                    metadata={
                        "manifest_digest": "sha256:" + "a" * 64,
                        "profile": "recommended",
                    },
                    completion_document=concurrent_receipt,
                    completion_digest=concurrent_digest,
                    created_at=created_at,
                )
                return report.revision_id

        with ThreadPoolExecutor(max_workers=2) as pool:
            revision_ids = tuple(pool.map(lambda _: publish(), range(2)))
        assert len(set(revision_ids)) == 1
        with database.connect() as connection:
            assert len(PostgresReportRevisionStore(connection).list(concurrent_campaign)) == 1

        conflicting_campaign = "campaign/concurrent-conflict"
        conflicting_receipt, conflicting_digest = _seal(conflicting_campaign)
        conflict_barrier = Barrier(2)

        def publish_conflict(index: int) -> str:
            with database.connect() as thread_connection:
                service = DurableReportService(PostgresReportRevisionStore(thread_connection))
                conflict_barrier.wait(timeout=5)
                return service.create(
                    conflicting_campaign,
                    stop_reason="capped",
                    cleanup_status="completed",
                    findings=(),
                    tasks=(),
                    evidence=(),
                    usage={},
                    metadata={
                        "manifest_digest": "sha256:" + "a" * 64,
                        "profile": "recommended",
                        "title": f"conflicting-{index}",
                    },
                    completion_document=conflicting_receipt,
                    completion_digest=conflicting_digest,
                    created_at=created_at,
                ).revision_id

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(publish_conflict, index) for index in range(2)]
            outcomes = []
            for future in futures:
                try:
                    outcomes.append(future.result())
                except ValueError as error:
                    outcomes.append(str(error))
        assert sum("concurrently published a different report" in row for row in outcomes) == 1
        with database.connect() as connection:
            assert len(PostgresReportRevisionStore(connection).list(conflicting_campaign)) == 1

        distinct_campaign = "campaign/concurrent-distinct-seals"
        first_distinct_receipt, first_distinct_digest = _seal(distinct_campaign)
        second_distinct_receipt = json.loads(json.dumps(first_distinct_receipt))
        second_distinct_receipt["document"]["target_id"] = "target-2"
        second_distinct_receipt["completion_digest"] = _digest(second_distinct_receipt["document"])
        second_distinct_digest = _digest(second_distinct_receipt)
        distinct_barrier = Barrier(2)

        def publish_distinct(receipt_and_digest: tuple[dict, str]) -> str:
            receipt_value, digest_value = receipt_and_digest
            with database.connect() as thread_connection:
                service = DurableReportService(PostgresReportRevisionStore(thread_connection))
                distinct_barrier.wait(timeout=5)
                return service.create(
                    distinct_campaign,
                    stop_reason="capped",
                    cleanup_status="completed",
                    findings=(),
                    tasks=(),
                    evidence=(),
                    usage={},
                    metadata={
                        "manifest_digest": "sha256:" + "a" * 64,
                        "profile": "recommended",
                    },
                    completion_document=receipt_value,
                    completion_digest=digest_value,
                    created_at=created_at,
                ).revision_id

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [
                pool.submit(publish_distinct, value)
                for value in (
                    (first_distinct_receipt, first_distinct_digest),
                    (second_distinct_receipt, second_distinct_digest),
                )
            ]
            distinct_outcomes = []
            for future in futures:
                try:
                    distinct_outcomes.append(future.result())
                except ValueError as error:
                    distinct_outcomes.append(str(error))
        assert (
            sum("conflicted with another immutable revision" in row for row in distinct_outcomes)
            == 1
        )
        with database.connect() as connection:
            assert len(PostgresReportRevisionStore(connection).list(distinct_campaign)) == 1

    with Database(DatabaseConfig(data_dir=data_dir)) as restarted:
        with restarted.connect() as connection:
            repository = PostgresReportRevisionStore(connection)
            reports = DurableReportService(repository)
            restored = repository.find_by_completion_digest(campaign_id, receipt_digest)
            assert restored is not None and restored.revision_id == first.revision_id
            assert reports.export(restored.revision_id, "json") == reports.export(
                restored.revision_id, "json"
            )
            for format in ("html", "markdown", "json", "csv"):
                assert reports.export(restored.revision_id, format)
            with pytest.raises(ValueError, match="cannot be shadowed"):
                reports.create(
                    campaign_id,
                    stop_reason="capped",
                    cleanup_status="completed",
                    findings=(),
                    tasks=(),
                    evidence=(),
                    usage={},
                    created_at=created_at + timedelta(days=1),
                )
            changed = reports.create(
                "campaign/legacy-report",
                stop_reason="interrupted",
                cleanup_status="pending",
                findings=(),
                tasks=(),
                evidence=(),
                usage={},
                limitations=("substantive restored delta",),
                created_at=created_at + timedelta(days=1),
            )
            comparison = reports.compare(legacy_revision_id, changed.revision_id)
            assert any("substantive restored delta" in row for row in comparison)
            assert all(
                token not in "\n".join(comparison)
                for token in ("revision_id", "created_at", "completion_digest")
            )
            legacy = repository.get(legacy_revision_id)
            assert legacy is not None and legacy.source_seal_state == "legacy_unsealed"
            assert "legacy_unsealed" in reports.export(legacy_revision_id, "json")

            connection.execute(
                """UPDATE merlin_storage.report_revisions
                   SET projection = jsonb_set(projection, '{campaign_id}', '"wrong"')
                   WHERE revision_id = %s""",
                (legacy_revision_id,),
            )
            with pytest.raises(ValueError, match="indexed report identity"):
                repository.get(legacy_revision_id)


def test_report_seals_revision_when_source_rows_carry_datetimes(tmp_path: Path) -> None:
    """Report generation must tolerate raw datetime values handed back by
    durable stores (cleanup-authority observed_at, scheduler timing) instead of
    crashing the JSONB write with "Object of type datetime is not JSON
    serializable" (campaign 1367294e's failed report retry)."""
    data_dir = tmp_path / "postgres"
    campaign_id = "campaign/report-datetime"
    created_at = datetime(2030, 1, 1, tzinfo=UTC)
    observed_at = datetime(2030, 1, 1, 12, 30, tzinfo=UTC)

    def _normalized(value: object) -> object:
        # The daemon seal normalizes datetime via isoformat; test digests must
        # be computed over the same normalized shape the service will see.
        return json.loads(json.dumps(value, default=lambda item: item.isoformat()))

    document = {
        "schema": "merlin.campaign-completion.v1",
        "campaign_id": campaign_id,
        "manifest_digest": "sha256:" + "a" * 64,
        "target_id": "target-1",
        "profile": "recommended",
        "cells": (),
        "waves": (),
        "receipts": (),
        "findings": (),
        "cleanup": {
            "expected": (),
            "states": (),
            "authority": {"status": "acknowledged", "observed_at": observed_at},
        },
        "saturation": {"ready": False},
    }
    receipt = {
        "kind": "campaign_completion",
        "schema": "merlin.campaign-completion.v1",
        "completion_digest": _digest(_normalized(document)),
        "terminal_status": "completed",
        "stop_reason": "capped",
        "document": document,
    }
    receipt_digest = _digest(_normalized(receipt))

    kwargs = {
        "stop_reason": "capped",
        "cleanup_status": "completed",
        "findings": (),
        "tasks": ({"id": "task-1", "observed_at": observed_at},),
        "evidence": (),
        "usage": {},
        "metadata": {
            "manifest_digest": "sha256:" + "a" * 64,
            "profile": "recommended",
        },
        "completion_document": receipt,
        "completion_digest": receipt_digest,
        "created_at": created_at,
    }
    with Database(DatabaseConfig(data_dir=data_dir)) as database:
        with database.connect() as connection:
            MigrationRunner(connection).apply()
            session = SessionRepository(connection).create("report datetime")
            CampaignStore(connection).prepare(
                campaign_id,
                str(session.id),
                {"manifest": {"objective": "report datetime"}, "assignment_context": {}},
                "sha256:" + "a" * 64,
            )
            reports = DurableReportService(PostgresReportRevisionStore(connection))
            first = reports.create(campaign_id, **kwargs)
            # The report-retry path re-creates the identical sealed report; the
            # datetime-carrying source rows must dedupe to the same revision.
            repeated = reports.create(campaign_id, **kwargs)
            assert repeated.revision_id == first.revision_id
            exported = reports.export(first.revision_id, "json")
            assert json.loads(exported)["campaign_id"] == campaign_id
        with database.connect() as connection:
            repository = PostgresReportRevisionStore(connection)
            restored = repository.find_by_completion_digest(campaign_id, receipt_digest)
            assert restored is not None and restored.revision_id == first.revision_id
            reports = DurableReportService(repository)
            assert (
                json.loads(reports.export(restored.revision_id, "json"))["tasks"][0]["observed_at"]
                == observed_at.isoformat()
            )
