from pathlib import Path
from uuid import UUID

from merlin_storage_postgres import Database, DatabaseConfig, MigrationRunner
from merlin_storage_postgres.campaign_store import CampaignStore


def _entry(**overrides: str) -> dict[str, str]:
    entry = {
        "target_id": "target-1",
        "adapter_generation": "generation-1",
        "technique_id": "technique-1",
        "controller_id": "goat",
        "payload_family": "translated",
        "language_id": "lzh",
        "mutation_id": "sha256:mutation-a",
        "outcome": "confirmed",
        "source_campaign_id": "campaign-1",
    }
    entry.update(overrides)
    return entry


def _record_a2a_attempt(
    connection,
    *,
    attempt_id: str,
    campaign_id: str,
    technique_id: str,
    deliveries: tuple[str, ...],
    state: str = "refuted",
    language: str = "lzh",
    strategy: str = "goat",
    control_kind: str = "attack",
) -> None:
    connection.execute(
        """INSERT INTO merlin_storage.campaign_technique_attempts
               (id, campaign_id, wave, task_id, technique_id, strategy_id,
                transform_id, language, control_kind, oracle_digest, state, activated_at,
                terminal_reason, terminal_at)
           VALUES (%s, %s, 1, %s, %s, %s, 'translated', %s, %s, %s,
                   %s, now(), 'test terminal', now())""",
        (
            UUID(attempt_id),
            campaign_id,
            f"task-{attempt_id[-1]}",
            technique_id,
            strategy,
            language,
            control_kind,
            "sha256:" + "1" * 64,
            state,
        ),
    )
    for index, delivery in enumerate(deliveries):
        connection.execute(
            """INSERT INTO merlin_storage.tool_effects
                   (idempotency_key, session_id, call_id, tool_name, effect,
                    arguments_digest, state, result_content, result_is_error,
                    result_metadata, campaign_id)
               VALUES (%s, %s, %s, 'target_chat', 'external', %s,
                       'acknowledged', '{}', false, %s::jsonb, %s)""",
            (
                f"effect-{attempt_id}-{index}",
                UUID("00000000-0000-0000-0000-000000000001"),
                f"call-{attempt_id}-{index}",
                "sha256:" + str(index) * 64,
                (
                    '{"interaction_role":"target_exchange",'
                    f'"technique_attempt_id":"{attempt_id}",'
                    '"target_delivery_receipt":'
                    f'{{"actual_delivery":"{delivery}"}}}}'
                ),
                campaign_id,
            ),
        )


def test_technique_history_preserves_terminal_result_from_nonterminal_overwrite(
    tmp_path: Path,
) -> None:
    with Database(DatabaseConfig(data_dir=tmp_path / "postgres")) as database:
        with database.connect() as connection:
            MigrationRunner(connection).apply()
            store = CampaignStore(connection)
            store.record_technique_history((_entry(),))
            store.record_technique_history(
                (
                    _entry(
                        outcome="unclear",
                        source_campaign_id="campaign-inconclusive",
                    ),
                )
            )

            rows = store.lookup_technique_history(
                "target-1",
                "generation-1",
                (("technique-1", "goat", "translated", "lzh", "sha256:mutation-a"),),
            )

            assert next(iter(rows.values())) == {
                "outcome": "confirmed",
                "source_campaign_id": "campaign-1",
            }


def test_technique_history_round_trips_partial_signal_and_resists_unclear_overwrite(
    tmp_path: Path,
) -> None:
    """A graded partial persists its signal payload; a later inconclusive
    campaign never downgrades it, while a terminal verdict still wins."""
    signal = {
        "progress": "target engaged across 2 observed attempts",
        "engaged_attempts": 2,
        "evidence_refs": ["ev-1", "ev-2"],
        "graded_by": "campaign_technique_attempts.state:observed",
    }
    cell = ("technique-1", "goat", "translated", "lzh", "sha256:mutation-a")
    with Database(DatabaseConfig(data_dir=tmp_path / "postgres")) as database:
        with database.connect() as connection:
            MigrationRunner(connection).apply()
            store = CampaignStore(connection)
            store.record_technique_history(
                (_entry(outcome="partial", source_campaign_id="campaign-1"),)
            )
            # The store accepts an explicit partial_signal payload.
            store.record_technique_history(
                (
                    {
                        **_entry(outcome="partial", source_campaign_id="campaign-1"),
                        "partial_signal": signal,
                    },
                )
            )
            store.record_technique_history(
                (_entry(outcome="unclear", source_campaign_id="campaign-2"),)
            )

            rows = store.lookup_technique_history("target-1", "generation-1", (cell,))

            assert rows[cell] == {
                "outcome": "partial",
                "source_campaign_id": "campaign-1",
                "partial_signal": {
                    **signal,
                    "evidence_refs": ["ev-1", "ev-2"],
                },
            }

            store.record_technique_history(
                (_entry(outcome="refuted", source_campaign_id="campaign-3"),)
            )
            rows = store.lookup_technique_history("target-1", "generation-1", (cell,))
            assert rows[cell]["outcome"] == "refuted"


def test_technique_history_stores_language_and_mutation_as_independent_cells(
    tmp_path: Path,
) -> None:
    with Database(DatabaseConfig(data_dir=tmp_path / "postgres")) as database:
        with database.connect() as connection:
            MigrationRunner(connection).apply()
            store = CampaignStore(connection)
            store.record_technique_history(
                (
                    _entry(),
                    _entry(
                        language_id="sw",
                        mutation_id="sha256:mutation-b",
                        outcome="refuted",
                        source_campaign_id="campaign-2",
                    ),
                )
            )

            cells = (
                ("technique-1", "goat", "translated", "lzh", "sha256:mutation-a"),
                ("technique-1", "goat", "translated", "sw", "sha256:mutation-b"),
            )
            rows = store.lookup_technique_history("target-1", "generation-1", cells)

            assert set(rows) == set(cells)
            assert rows[cells[0]]["outcome"] == "confirmed"
            assert rows[cells[1]]["outcome"] == "refuted"


def test_a2a_history_projection_requires_authenticated_peer_only_delivery(
    tmp_path: Path,
) -> None:
    legacy_id = "merlin-injection-taxonomy/cat/agent-to-agent/agent-goal-handoff"
    peer_id = "merlin-injection-taxonomy/cat/agent-to-agent/agent-message-override"
    mixed_id = "merlin-injection-taxonomy/cat/agent-to-agent/agent-split-relay"
    direct_id = "merlin-injection-taxonomy/cat/agent-to-agent/agent-message-exfil"
    sibling_id = "merlin-injection-taxonomy/cat/agent-to-agent/agent-authority-relay"
    mismatch_id = "merlin-injection-taxonomy/cat/agent-to-agent/agent-encoded-relay"
    with Database(DatabaseConfig(data_dir=tmp_path / "postgres")) as database:
        with database.connect() as connection:
            MigrationRunner(connection).apply()
            connection.execute(
                "INSERT INTO merlin_storage.sessions (id, title) VALUES (%s, 'history-test')",
                (UUID("00000000-0000-0000-0000-000000000001"),),
            )
            store = CampaignStore(connection)
            store.record_technique_history(
                (
                    _entry(
                        technique_id=legacy_id,
                        outcome="confirmed",
                        mutation_id="none",
                        source_campaign_id="campaign-legacy",
                    ),
                    _entry(
                        technique_id=peer_id,
                        outcome="refuted",
                        mutation_id="none",
                        source_campaign_id="campaign-peer",
                    ),
                    _entry(
                        technique_id=mixed_id,
                        outcome="confirmed",
                        mutation_id="none",
                        source_campaign_id="campaign-mixed",
                    ),
                    _entry(
                        technique_id=direct_id,
                        outcome="refuted",
                        mutation_id="none",
                        source_campaign_id="campaign-direct",
                    ),
                    _entry(
                        technique_id=sibling_id,
                        outcome="confirmed",
                        mutation_id="none",
                        source_campaign_id="campaign-sibling",
                    ),
                    _entry(
                        technique_id=peer_id,
                        language_id="sw",
                        outcome="confirmed",
                        mutation_id="none",
                        source_campaign_id="campaign-peer",
                    ),
                    _entry(
                        technique_id=mismatch_id,
                        outcome="confirmed",
                        mutation_id="none",
                        source_campaign_id="campaign-mismatch",
                    ),
                    _entry(
                        technique_id=mismatch_id,
                        language_id="sw",
                        outcome="confirmed",
                        mutation_id="none",
                        source_campaign_id="campaign-confirmed",
                    ),
                    _entry(
                        technique_id=mismatch_id,
                        controller_id="direct",
                        outcome="confirmed",
                        mutation_id="none",
                        source_campaign_id="campaign-inconclusive",
                    ),
                    _entry(
                        technique_id=mismatch_id,
                        controller_id="hydra-select",
                        outcome="confirmed",
                        mutation_id="none",
                        source_campaign_id="campaign-cancelled",
                    ),
                    _entry(
                        technique_id=mismatch_id,
                        controller_id="crescendo",
                        outcome="confirmed",
                        mutation_id="none",
                        source_campaign_id="campaign-control",
                    ),
                )
            )
            _record_a2a_attempt(
                connection,
                attempt_id="00000000-0000-0000-0000-000000000011",
                campaign_id="campaign-peer",
                technique_id=peer_id,
                deliveries=("inter-agent-message",),
            )
            _record_a2a_attempt(
                connection,
                attempt_id="00000000-0000-0000-0000-000000000012",
                campaign_id="campaign-mixed",
                technique_id=mixed_id,
                deliveries=("inter-agent-message", "direct-user-channel"),
                state="confirmed",
            )
            _record_a2a_attempt(
                connection,
                attempt_id="00000000-0000-0000-0000-000000000013",
                campaign_id="campaign-direct",
                technique_id=direct_id,
                deliveries=("direct-user-channel",),
            )
            _record_a2a_attempt(
                connection,
                attempt_id="00000000-0000-0000-0000-000000000014",
                campaign_id="campaign-sibling",
                technique_id=sibling_id,
                deliveries=("inter-agent-message",),
                state="confirmed",
            )
            _record_a2a_attempt(
                connection,
                attempt_id="00000000-0000-0000-0000-000000000015",
                campaign_id="campaign-sibling",
                technique_id=sibling_id,
                deliveries=("direct-user-channel",),
                state="confirmed",
            )
            _record_a2a_attempt(
                connection,
                attempt_id="00000000-0000-0000-0000-000000000016",
                campaign_id="campaign-mismatch",
                technique_id=mismatch_id,
                deliveries=("inter-agent-message",),
                state="refuted",
            )
            _record_a2a_attempt(
                connection,
                attempt_id="00000000-0000-0000-0000-000000000017",
                campaign_id="campaign-confirmed",
                technique_id=mismatch_id,
                deliveries=("inter-agent-message",),
                state="confirmed",
                language="sw",
            )
            _record_a2a_attempt(
                connection,
                attempt_id="00000000-0000-0000-0000-000000000018",
                campaign_id="campaign-inconclusive",
                technique_id=mismatch_id,
                deliveries=("inter-agent-message",),
                state="inconclusive",
                strategy="direct",
            )
            _record_a2a_attempt(
                connection,
                attempt_id="00000000-0000-0000-0000-000000000019",
                campaign_id="campaign-cancelled",
                technique_id=mismatch_id,
                deliveries=("inter-agent-message",),
                state="cancelled",
                strategy="hydra-select",
            )
            _record_a2a_attempt(
                connection,
                attempt_id="00000000-0000-0000-0000-000000000020",
                campaign_id="campaign-control",
                technique_id=mismatch_id,
                deliveries=("inter-agent-message",),
                state="confirmed",
                strategy="crescendo",
                control_kind="negative_control",
            )
            cells = tuple(
                (technique_id, "goat", "translated", "lzh", "none")
                for technique_id in (legacy_id, peer_id, mixed_id, direct_id, sibling_id)
            )
            cross_variant_cell = (peer_id, "goat", "translated", "sw", "none")
            mismatch_cell = (mismatch_id, "goat", "translated", "lzh", "none")
            confirmed_cell = (mismatch_id, "goat", "translated", "sw", "none")
            invalid_terminal_cells = (
                (mismatch_id, "direct", "translated", "lzh", "none"),
                (mismatch_id, "hydra-select", "translated", "lzh", "none"),
                (mismatch_id, "crescendo", "translated", "lzh", "none"),
            )
            cells = (
                *cells,
                cross_variant_cell,
                mismatch_cell,
                confirmed_cell,
                *invalid_terminal_cells,
            )

            rows = store.lookup_technique_history("target-1", "generation-1", cells)

            assert rows[cells[0]]["outcome"] == "unclear"
            assert rows[cells[0]]["recorded_outcome"] == "confirmed"
            assert rows[cells[0]]["retryable"] is True
            assert rows[cells[0]]["evaluation_uncertainties"][0]["kind"] == ("evaluation_uncertain")
            assert rows[cells[1]] == {
                "outcome": "refuted",
                "source_campaign_id": "campaign-peer",
                "peer_delivery_authenticated": True,
            }
            assert rows[cells[2]]["outcome"] == "unclear"
            assert rows[cells[2]]["recorded_outcome"] == "confirmed"
            assert rows[cells[2]]["peer_delivery_authenticated"] is False
            assert rows[cells[3]]["outcome"] == "unclear"
            assert rows[cells[3]]["recorded_outcome"] == "refuted"
            assert rows[cells[3]]["peer_delivery_authenticated"] is False
            assert rows[cells[4]]["outcome"] == "unclear"
            assert rows[cells[4]]["recorded_outcome"] == "confirmed"
            assert rows[cells[4]]["peer_delivery_authenticated"] is False
            assert rows[cross_variant_cell]["outcome"] == "unclear"
            assert rows[cross_variant_cell]["recorded_outcome"] == "confirmed"
            assert rows[cross_variant_cell]["peer_delivery_authenticated"] is False
            assert rows[mismatch_cell]["outcome"] == "unclear"
            assert rows[mismatch_cell]["recorded_outcome"] == "confirmed"
            assert rows[mismatch_cell]["peer_delivery_authenticated"] is False
            assert rows[confirmed_cell] == {
                "outcome": "confirmed",
                "source_campaign_id": "campaign-confirmed",
                "peer_delivery_authenticated": True,
            }
            for cell in invalid_terminal_cells:
                assert rows[cell]["outcome"] == "unclear"
                assert rows[cell]["recorded_outcome"] == "confirmed"
                assert rows[cell]["peer_delivery_authenticated"] is False
            raw = connection.execute(
                """SELECT outcome FROM merlin_storage.technique_history
                   WHERE technique_id IN (%s, %s, %s) ORDER BY technique_id""",
                (legacy_id, mixed_id, direct_id),
            ).fetchall()
            assert sorted(str(row["outcome"]) for row in raw) == [
                "confirmed",
                "confirmed",
                "refuted",
            ]
            connection.execute("SET enable_seqscan = off")
            try:
                plan = connection.execute(
                    """EXPLAIN (COSTS OFF)
                       SELECT idempotency_key
                       FROM merlin_storage.tool_effects
                       WHERE campaign_id = %s
                         AND result_metadata ->> 'technique_attempt_id' = %s
                         AND result_metadata ->> 'interaction_role' = 'target_exchange'
                         AND state IN ('acknowledged', 'reconciled')""",
                    (
                        "campaign-peer",
                        "00000000-0000-0000-0000-000000000011",
                    ),
                ).fetchall()
            finally:
                connection.execute("RESET enable_seqscan")
            assert "tool_effects_campaign_technique_attempt_target_idx" in "\n".join(
                str(row["QUERY PLAN"]) for row in plan
            )
