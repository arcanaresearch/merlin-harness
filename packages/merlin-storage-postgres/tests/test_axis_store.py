from pathlib import Path

from merlin_storage_postgres import Database, DatabaseConfig, MigrationRunner
from merlin_storage_postgres.arm_store import CampaignArmStore, CampaignAxisStore


def _axis_value(**overrides: object) -> dict[str, object]:
    value: dict[str, object] = {
        "axis": "register",
        "slug": "formal",
        "display_name": "Formal",
        "instruction_fragment": "Adopt a formal register.",
        "origin": "axis-expansion",
        "obscurity": 3,
    }
    value.update(overrides)
    return value


def _arm(**overrides: object) -> dict[str, object]:
    arm: dict[str, object] = {
        "id": "arm/one",
        "target_id": "target-1",
        "scenario_fingerprint": "sha256:scenario-one",
        "surface_id": "chat",
        "semantic_id": "semantic/one",
        "strategy_id": "direct",
        "technique_id": "technique/one",
        "controller_id": "direct",
        "mutation_id": "mutation/none",
        "actual_language": "en",
        "state_id": "fresh-session",
        "objective": "Probe the authentication boundary.",
        "source": "brainstorm",
        "novelty_digest": "sha256:novelty",
        "status": "trial",
        "rank_components": {"security_value": 0.7},
        "eligibility_reasons": ["authorized", "deliverable"],
        "decision_reason": "Axis-composed candidate is eligible for trial.",
        "axis_coords": {
            "register": "formal",
            "framing": "authority",
            "encoding": "none",
            "structure": "single_shot",
        },
    }
    arm.update(overrides)
    return arm


def test_pg0_axis_store_inserts_idempotently_and_lists_axes(tmp_path: Path) -> None:
    with Database(DatabaseConfig(data_dir=tmp_path / "postgres")) as database:
        with database.connect() as connection:
            MigrationRunner(connection).apply()
            store = CampaignAxisStore(connection)
            store.insert_axis_value(_axis_value())
            # Same (axis, slug) but a different fragment: replay must be a no-op.
            store.insert_axis_value(
                _axis_value(slug="formal", instruction_fragment="A different fragment.")
            )
            store.insert_axis_value(
                _axis_value(axis="framing", slug="authority", display_name="Authority")
            )

            assert store.list_axes() == ["framing", "register"]
            values = store.list_axis_values("register")
            assert [value["slug"] for value in values] == ["formal"]
            assert values[0]["origin"] == "axis-expansion"
            assert values[0]["obscurity"] == 3
            assert values[0]["instruction_fragment"] == "Adopt a formal register."


def test_pg0_axis_store_scopes_values_by_axis(tmp_path: Path) -> None:
    with Database(DatabaseConfig(data_dir=tmp_path / "postgres")) as database:
        with database.connect() as connection:
            MigrationRunner(connection).apply()
            store = CampaignAxisStore(connection)
            store.insert_axis_value(_axis_value(axis="register", slug="formal"))
            store.insert_axis_value(_axis_value(axis="framing", slug="authority"))

            assert [value["slug"] for value in store.list_axis_values("register")] == ["formal"]
            assert [value["slug"] for value in store.list_axis_values("framing")] == ["authority"]
            assert store.list_axis_values("encoding") == []


def test_pg0_axis_coords_round_trips_through_campaign_arm_store(tmp_path: Path) -> None:
    with Database(DatabaseConfig(data_dir=tmp_path / "postgres")) as database:
        with database.connect() as connection:
            MigrationRunner(connection).apply()
            store = CampaignArmStore(connection)
            store.record_arm(_arm())

            arms = store.list_arms("target-1", "sha256:scenario-one")
            assert len(arms) == 1
            assert arms[0]["axis_coords"] == {
                "register": "formal",
                "framing": "authority",
                "encoding": "none",
                "structure": "single_shot",
            }


def test_pg0_axis_coords_optional_on_record(tmp_path: Path) -> None:
    with Database(DatabaseConfig(data_dir=tmp_path / "postgres")) as database:
        with database.connect() as connection:
            MigrationRunner(connection).apply()
            store = CampaignArmStore(connection)
            store.record_arm(_arm(axis_coords=None))

            arms = store.list_arms("target-1", "sha256:scenario-one")
            assert len(arms) == 1
            assert arms[0]["axis_coords"] is None
