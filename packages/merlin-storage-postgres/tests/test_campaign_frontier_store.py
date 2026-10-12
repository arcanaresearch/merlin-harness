from __future__ import annotations

import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from merlin_storage_postgres import (
    CampaignStore,
    Database,
    DatabaseConfig,
    DurableSchedulerStore,
    MigrationRunner,
    SessionRepository,
)
from psycopg.errors import ForeignKeyViolation


def _frontier() -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    families = [
        {
            "id": "technique/prompt-injection",
            "document": {
                "id": "technique/prompt-injection",
                "objective": "Probe instruction priority",
                "content_ids": ["case:prompt-injection"],
            },
        }
    ]
    candidates = [
        {
            "id": "technique/prompt-injection@lang=en@variant=native",
            "family_id": "technique/prompt-injection",
            "ordinal": 0,
            "requested_language": "en",
            "variant_id": "native",
            "variant": {"language": "en", "variant": "native"},
            "priority": 0.4,
        },
        {
            "id": "technique/prompt-injection@lang=ms@variant=translated",
            "family_id": "technique/prompt-injection",
            "ordinal": 1,
            "requested_language": "ms",
            "variant_id": "translated",
            "variant": {"language": "ms", "variant": "translated"},
            "priority": 0.9,
        },
        {
            "id": "technique/prompt-injection@lang=ja@variant=script-switching",
            "family_id": "technique/prompt-injection",
            "ordinal": 2,
            "requested_language": "ja",
            "variant_id": "script-switching",
            "variant": {"language": "ja", "variant": "script-switching"},
            "state": "dormant",
        },
    ]
    return families, candidates


def _prepare(connection, *, campaign_id: str = "campaign/frontier") -> tuple[CampaignStore, str]:
    MigrationRunner(connection).apply()
    session = SessionRepository(connection).create("campaign frontier")
    store = CampaignStore(connection)
    store.prepare(campaign_id, str(session.id), {"manifest": {}}, "sha256:frontier")
    return store, str(session.id)


def test_frontier_registration_is_compact_idempotent_and_exact(tmp_path: Path) -> None:
    with Database(DatabaseConfig(data_dir=tmp_path / "postgres-frontier-register")) as database:
        with database.connect() as connection:
            store, session_id = _prepare(connection)
            families, candidates = _frontier()

            assert store.register_frontier("campaign/frontier", families, candidates) == {
                "dormant": 1,
                "queued": 2,
                "claimed": 0,
                "scheduled": 0,
                "terminal": 0,
                "total": 3,
            }
            assert store.register_frontier("campaign/frontier", families, candidates)["total"] == 3
            family_count = connection.execute(
                """SELECT count(*) AS count
                   FROM merlin_storage.campaign_frontier_families
                   WHERE campaign_id = 'campaign/frontier'"""
            ).fetchone()
            assert family_count["count"] == 1

            conflicting = [{**candidates[0], "requested_language": "fr"}]
            with pytest.raises(ValueError, match="conflicts with durable state"):
                store.register_frontier("campaign/frontier", families, conflicting)

            store.prepare(
                "campaign/frontier-scale",
                session_id,
                {"manifest": {}},
                "sha256:frontier-scale",
            )
            scale_candidates = [
                {
                    "id": f"technique/prompt-injection@lang=x-{index}@variant=native",
                    "family_id": "technique/prompt-injection",
                    "ordinal": index,
                    "requested_language": f"x-{index}",
                    "variant_id": "native",
                }
                for index in range(12_765)
            ]
            scale_counts = store.register_frontier(
                "campaign/frontier-scale", families, scale_candidates
            )
            assert scale_counts["queued"] == 12_765
            assert scale_counts["total"] == 12_765


def test_prepare_with_frontier_rolls_back_and_retries_atomically(tmp_path: Path) -> None:
    with Database(DatabaseConfig(data_dir=tmp_path / "postgres-frontier-prepare")) as database:
        with database.connect() as connection:
            MigrationRunner(connection).apply()
            session = SessionRepository(connection).create("atomic frontier prepare")
            store = CampaignStore(connection)
            families, candidates = _frontier()
            manifest = {"manifest": {"objective": "atomic frontier"}}
            invalid = [{**candidates[0], "family_id": "missing-family"}]

            with pytest.raises(ValueError, match="unknown family"):
                store.prepare_with_frontier(
                    "campaign/atomic-frontier",
                    str(session.id),
                    manifest,
                    "sha256:atomic-frontier",
                    families,
                    invalid,
                )
            assert store.get("campaign/atomic-frontier") is None

            prepared = store.prepare_with_frontier(
                "campaign/atomic-frontier",
                str(session.id),
                manifest,
                "sha256:atomic-frontier",
                families,
                candidates,
            )
            assert prepared["frontier_counts"]["total"] == 3
            replayed = store.prepare_with_frontier(
                "campaign/atomic-frontier",
                str(session.id),
                manifest,
                "sha256:atomic-frontier",
                families,
                candidates,
            )
            assert replayed["frontier_counts"] == prepared["frontier_counts"]

            with pytest.raises(ValueError, match="identity conflicts"):
                store.prepare_with_frontier(
                    "campaign/atomic-frontier",
                    str(session.id),
                    {"manifest": {"objective": "different"}},
                    "sha256:different",
                    families,
                    candidates,
                )


def test_frontier_shortlist_priority_update_changes_next_claim(tmp_path: Path) -> None:
    with Database(DatabaseConfig(data_dir=tmp_path / "postgres-frontier-priority")) as database:
        with database.connect() as connection:
            store, _session_id = _prepare(connection)
            families, candidates = _frontier()
            queued = [
                {**candidate, "priority": None, "state": "queued"}
                for candidate in candidates
            ]
            store.register_frontier("campaign/frontier", families, queued)

            shortlist = store.list_frontier_shortlist("campaign/frontier", 2)
            assert tuple(item["ordinal"] for item in shortlist) == (0, 1)
            assert tuple(
                item["ordinal"]
                for item in store.list_frontier_shortlist(
                    "campaign/frontier", 2, after_ordinal=0
                )
            ) == (1, 2)
            selected_id = str(queued[2]["id"])
            assert store.update_frontier_priorities(
                "campaign/frontier", {selected_id: 1.0}
            ) == 1

            batch = store.claim_frontier_batch("campaign/frontier", 0, "batch/priority", 1)
            assert tuple(item["id"] for item in batch["candidates"]) == (selected_id,)


def test_exact_frontier_claim_is_ordered_idempotent_and_concurrent(tmp_path: Path) -> None:
    with Database(DatabaseConfig(data_dir=tmp_path / "postgres-frontier-exact")) as database:
        with database.connect() as connection:
            store, _session_id = _prepare(connection)
            families, candidates = _frontier()
            store.register_frontier("campaign/frontier", families, candidates[:2])

        first_ids = (str(candidates[1]["id"]), str(candidates[0]["id"]))

        def claim(batch_id: str, wave: int, ids: tuple[str, ...]):
            try:
                with database.connect() as connection:
                    return CampaignStore(connection).claim_frontier_batch_exact(
                        "campaign/frontier", wave, batch_id, ids
                    )
            except ValueError as error:
                return error

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(
                executor.map(
                    lambda item: claim(*item),
                    (
                        ("batch/exact-a", 0, first_ids),
                        ("batch/exact-b", 1, tuple(reversed(first_ids))),
                    ),
                )
            )
        successes = [result for result in results if isinstance(result, dict)]
        failures = [result for result in results if isinstance(result, ValueError)]
        assert len(successes) == 1
        assert len(failures) == 1
        assert "active frontier batch" in str(failures[0])
        winner = successes[0]
        winner_ids = tuple(str(item["id"]) for item in winner["candidates"])
        expected_ids = first_ids if winner["id"] == "batch/exact-a" else tuple(reversed(first_ids))
        assert winner_ids == expected_ids

        with database.connect() as connection:
            store = CampaignStore(connection)
            assert store.claim_frontier_batch_exact(
                "campaign/frontier", int(winner["wave"]), str(winner["id"]), winner_ids
            ) == winner
            with pytest.raises(ValueError, match="different exact claim"):
                store.claim_frontier_batch_exact(
                    "campaign/frontier",
                    int(winner["wave"]),
                    str(winner["id"]),
                    tuple(reversed(winner_ids)),
                )


def test_frontier_claim_resize_bind_and_settle_are_restart_safe(tmp_path: Path) -> None:
    data_dir = tmp_path / "postgres-frontier-lifecycle"
    with Database(DatabaseConfig(data_dir=data_dir)) as database:
        with database.connect() as connection:
            store, session_id = _prepare(connection)
            families, candidates = _frontier()
            store.register_frontier("campaign/frontier", families, candidates)

            batch = store.claim_frontier_batch("campaign/frontier", 0, "batch/0", 2)
            claimed_ids = tuple(item["id"] for item in batch["candidates"])
            assert claimed_ids == (
                "technique/prompt-injection@lang=ms@variant=translated",
                "technique/prompt-injection@lang=en@variant=native",
            )
            assert batch["candidate_digest"] == hashlib.sha256(
                json.dumps(claimed_ids, separators=(",", ":")).encode()
            ).hexdigest()
            assert store.claim_frontier_batch("campaign/frontier", 0, "batch/0", 1) == batch
            assert store.load_active_frontier_batch("campaign/frontier") == batch

            resized = store.resize_frontier_batch("campaign/frontier", "batch/0", 1)
            assert tuple(item["id"] for item in resized["candidates"]) == claimed_ids[:1]
            assert store.frontier_counts("campaign/frontier")["queued"] == 1

            scheduler = DurableSchedulerStore(connection)
            schedule_id = scheduler.create_schedule(
                session_id,
                {"assignments": []},
                {"states": {}},
                ["lead"],
                ready_nodes=["lead"],
            )
            scheduled = store.bind_frontier_batch(
                "campaign/frontier", "batch/0", schedule_id
            )
            assert scheduled["state"] == "scheduled"
            assert scheduled["schedule_id"] == schedule_id
            assert store.bind_frontier_batch(
                "campaign/frontier", "batch/0", schedule_id
            ) == scheduled

    with Database(DatabaseConfig(data_dir=data_dir)) as restarted:
        with restarted.connect() as connection:
            MigrationRunner(connection).apply()
            store = CampaignStore(connection)
            active = store.load_active_frontier_batch("campaign/frontier")
            assert active is not None
            candidate_id = str(active["candidates"][0]["id"])
            counts = store.settle_frontier_batch(
                "campaign/frontier",
                "batch/0",
                {candidate_id: "retryable"},
            )
            assert counts == {
                "dormant": 1,
                "queued": 2,
                "claimed": 0,
                "scheduled": 0,
                "terminal": 0,
                "total": 3,
            }
            assert store.load_active_frontier_batch("campaign/frontier") is None
            assert store.settle_frontier_batch(
                "campaign/frontier", "batch/0", {candidate_id: "retryable"}
            ) == counts
            with pytest.raises(ValueError, match="already settled differently"):
                store.settle_frontier_batch(
                    "campaign/frontier",
                    "batch/0",
                    {candidate_id: {"disposition": "terminal", "reason": "done"}},
                )


def test_frontier_terminal_and_empty_batches_preserve_coverage(tmp_path: Path) -> None:
    with Database(DatabaseConfig(data_dir=tmp_path / "postgres-frontier-terminal")) as database:
        with database.connect() as connection:
            store, session_id = _prepare(connection)
            families, candidates = _frontier()
            store.register_frontier("campaign/frontier", families, candidates[:1])
            batch = store.claim_frontier_batch("campaign/frontier", 0, "batch/0", 8)
            scheduler = DurableSchedulerStore(connection)
            schedule_id = scheduler.create_schedule(
                session_id,
                {"assignments": []},
                {"states": {}},
                ["lead"],
                ready_nodes=["lead"],
            )
            store.bind_frontier_batch("campaign/frontier", "batch/0", schedule_id)
            candidate_id = str(batch["candidates"][0]["id"])
            counts = store.settle_frontier_batch(
                "campaign/frontier",
                "batch/0",
                {candidate_id: {"disposition": "terminal", "reason": "clean negative"}},
            )
            assert counts["terminal"] == 1

            empty = store.claim_frontier_batch("campaign/frontier", 1, "batch/1", 8)
            assert empty["state"] == "settled"
            assert empty["candidates"] == ()
            assert empty["candidate_digest"] == hashlib.sha256(b"[]").hexdigest()


def test_campaign_store_persists_frontier_successor_wave(tmp_path: Path) -> None:
    with Database(DatabaseConfig(data_dir=tmp_path / "postgres-frontier-wave")) as database:
        with database.connect() as connection:
            store, session_id = _prepare(connection)
            scheduler = DurableSchedulerStore(connection)
            initial_schedule = scheduler.create_schedule(
                session_id,
                {"assignments": []},
                {"states": {}},
                ["initial"],
                ready_nodes=["initial"],
            )
            frontier_schedule = scheduler.create_schedule(
                session_id,
                {"assignments": []},
                {"states": {}},
                ["frontier"],
                ready_nodes=["frontier"],
            )

            store.add_wave("campaign/frontier", 0, "initial", initial_schedule)
            frontier = store.add_wave(
                "campaign/frontier", 1, "frontier", frontier_schedule
            )

            assert frontier["wave"] == 1
            assert frontier["kind"] == "frontier"
            assert str(frontier["schedule_id"]) == frontier_schedule
            assert [wave["kind"] for wave in store.list_waves("campaign/frontier")] == [
                "initial",
                "frontier",
            ]


def test_frontier_claim_serializes_competing_batches(tmp_path: Path) -> None:
    with Database(DatabaseConfig(data_dir=tmp_path / "postgres-frontier-concurrent")) as database:
        with database.connect() as connection:
            store, _session_id = _prepare(connection)
            families, candidates = _frontier()
            store.register_frontier("campaign/frontier", families, candidates[:2])

        def claim(batch_id: str, wave: int):
            try:
                with database.connect() as connection:
                    return CampaignStore(connection).claim_frontier_batch(
                        "campaign/frontier", wave, batch_id, 1
                    )
            except ValueError as error:
                return error

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(
                executor.map(lambda item: claim(*item), (("batch/a", 0), ("batch/b", 1)))
            )

        successes = [result for result in results if isinstance(result, dict)]
        assert len(successes) == 1
        failures = [result for result in results if isinstance(result, ValueError)]
        assert len(failures) == 1
        assert "active frontier batch" in str(failures[0])


def test_frontier_completion_snapshot_is_atomic_idempotent_and_exact(tmp_path: Path) -> None:
    with Database(DatabaseConfig(data_dir=tmp_path / "postgres-frontier-completion")) as database:
        with database.connect() as connection:
            store, _ = _prepare(connection)
            families, candidates = _frontier()
            store.register_frontier("campaign/frontier", families, candidates)
            rows = [
                {
                    "candidate_id": candidates[0]["id"],
                    "state": "terminal",
                    "outcome": "confirmed",
                    "task_id": "task/confirmed",
                    "evidence_refs": ["effect/1", "finding/1"],
                    "disposition_reason": "independently reproduced",
                },
                {
                    "candidate_id": candidates[1]["id"],
                    "state": "queued",
                    "outcome": "retryable",
                    "disposition_reason": "provider unavailable",
                },
                {
                    "candidate_id": candidates[2]["id"],
                    "state": "queued",
                    "outcome": "unattempted",
                },
            ]

            with pytest.raises(ForeignKeyViolation):
                store.record_frontier_completion_snapshot(
                    "campaign/frontier",
                    "completion/atomic",
                    [{**rows[0], "candidate_id": "missing-candidate"}],
                    wave=0,
                )
            assert store.get_frontier_completion_snapshot(
                "campaign/frontier", "completion/atomic"
            ) is None

            header = store.record_frontier_completion_snapshot(
                "campaign/frontier", "completion/atomic", rows, wave=0
            )
            assert header["kind"] == "wave"
            assert header["wave"] == 0
            assert header["revision"] == 1
            assert header["previous_snapshot_id"] is None
            assert header["total_count"] == 3
            assert header["counts"] == {
                "states": {"queued": 2, "claimed": 0, "scheduled": 0, "terminal": 1},
                "outcomes": {
                    "confirmed": 1,
                    "refuted": 0,
                    "partial": 0,
                    "unclear": 0,
                    "blocked": 0,
                    "retryable": 1,
                    "unattempted": 1,
                },
            }
            assert (
                store.record_frontier_completion_snapshot(
                    "campaign/frontier", "completion/atomic", rows, wave=0
                )["row_digest"]
                == header["row_digest"]
            )
            assert store.frontier_completion_counts("campaign/frontier", "completion/atomic") == {
                key: header[key]
                for key in (
                    "id",
                    "kind",
                    "wave",
                    "revision",
                    "previous_snapshot_id",
                    "row_digest",
                    "total_count",
                    "counts",
                )
            }
            page = store.list_frontier_completion_page(
                "campaign/frontier", "completion/atomic", limit=2
            )
            assert [row["position"] for row in page] == [0, 1]
            assert page[0]["evidence_refs"] == ("effect/1", "finding/1")
            assert (
                store.list_frontier_completion_page(
                    "campaign/frontier",
                    "completion/atomic",
                    limit=2,
                    after_position=page[-1]["position"],
                )[0]["candidate_id"]
                == candidates[2]["id"]
            )

            with pytest.raises(ValueError, match="conflicts with durable state"):
                store.record_frontier_completion_snapshot(
                    "campaign/frontier", "completion/atomic", list(reversed(rows)), wave=0
                )
            revised = store.record_frontier_completion_snapshot(
                "campaign/frontier", "completion/other", list(reversed(rows)), wave=0
            )
            assert revised["revision"] == 2
            assert revised["previous_snapshot_id"] == "completion/atomic"
            assert store.get_current_frontier_completion_snapshot(
                "campaign/frontier", wave=0
            ) == revised
            assert tuple(
                header["id"]
                for header in store.iter_frontier_completion_history(
                    "campaign/frontier", page_size=1
                )
            ) == ("completion/atomic", "completion/other")
            with pytest.raises(ValueError, match="limit 1..500"):
                store.list_frontier_completion_page(
                    "campaign/frontier", "completion/atomic", limit=501
                )


def test_frontier_completion_scale_keeps_headers_bounded_and_pages_exactly(
    tmp_path: Path,
) -> None:
    with Database(DatabaseConfig(data_dir=tmp_path / "postgres-frontier-completion-scale")) as db:
        with db.connect() as connection:
            store, _ = _prepare(connection, campaign_id="campaign/frontier-completion-scale")
            families, _ = _frontier()
            candidates = [
                {
                    "id": f"technique/prompt-injection@lang=x-{index}@variant=native",
                    "family_id": "technique/prompt-injection",
                    "ordinal": index,
                    "requested_language": f"x-{index}",
                    "variant_id": "native",
                }
                for index in range(12_765)
            ]
            store.register_frontier("campaign/frontier-completion-scale", families, candidates)
            rows = [
                {
                    "candidate_id": candidate["id"],
                    "state": "terminal" if index < 65 else "queued",
                    "outcome": "refuted" if index < 65 else "unattempted",
                    "disposition_reason": ("authoritative target result" if index < 65 else None),
                }
                for index, candidate in enumerate(candidates)
            ]
            header = store.record_frontier_completion_snapshot(
                "campaign/frontier-completion-scale", "completion/final", rows
            )

            assert header["kind"] == "final"
            assert header["wave"] is None
            assert header["total_count"] == 12_765
            assert header["counts"]["states"]["terminal"] == 65
            assert header["counts"]["outcomes"]["unattempted"] == 12_700
            assert len(json.dumps(header, default=str)) < 2_048
            stored = connection.execute(
                """SELECT count(*) AS count
                   FROM merlin_storage.campaign_frontier_completion_rows
                   WHERE snapshot_id = 'completion/final'"""
            ).fetchone()
            assert stored["count"] == 12_765

            first = store.list_frontier_completion_page(
                "campaign/frontier-completion-scale", "completion/final", limit=37
            )
            second = store.list_frontier_completion_page(
                "campaign/frontier-completion-scale",
                "completion/final",
                limit=37,
                after_position=first[-1]["position"],
            )
            assert len(first) == len(second) == 37
            assert [row["position"] for row in first] == list(range(37))
            assert [row["position"] for row in second] == list(range(37, 74))

            history = store.list_frontier_completion_history(
                "campaign/frontier-completion-scale", limit=1
            )
            assert history == (header,)
            assert tuple(
                store.iter_frontier_completion_history(
                    "campaign/frontier-completion-scale", page_size=1
                )
            ) == (header,)
            assert "rows" not in header
