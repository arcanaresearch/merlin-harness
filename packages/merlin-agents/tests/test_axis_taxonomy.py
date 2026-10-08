from merlin_agents.axis_taxonomy import (
    AXES,
    axis_coords_key,
    enumerate_empty_cells,
)


def _axis_value(axis: str, slug: str) -> dict[str, object]:
    return {
        "axis": axis,
        "slug": slug,
        "display_name": slug.replace("_", " ").title(),
        "instruction_fragment": f"Do {slug}.",
        "origin": "seed",
        "obscurity": 3,
    }


def test_axes_are_the_four_dimensions() -> None:
    assert AXES == ("register", "framing", "encoding", "structure")


def test_axis_coords_key_joins_all_four() -> None:
    coords = {
        "register": "formal",
        "framing": "authority",
        "encoding": "none",
        "structure": "single_shot",
    }
    assert axis_coords_key(coords) == "formal|authority|none|single_shot"


def test_axis_coords_key_returns_none_when_any_member_is_missing() -> None:
    assert axis_coords_key(None) is None
    assert axis_coords_key({}) is None
    assert axis_coords_key({"register": "formal"}) is None
    assert (
        axis_coords_key({"register": "formal", "framing": "authority", "encoding": "none"}) is None
    )


def test_axis_coords_key_treats_empty_and_whitespace_as_missing() -> None:
    assert (
        axis_coords_key(
            {
                "register": "formal",
                "framing": "authority",
                "encoding": "none",
                "structure": " ",
            }
        )
        is None
    )
    assert (
        axis_coords_key(
            {
                "register": "formal",
                "framing": "authority",
                "encoding": "",
                "structure": "single_shot",
            }
        )
        is None
    )


def test_axis_coords_key_ignores_extra_members() -> None:
    coords = {
        "register": "formal",
        "framing": "authority",
        "encoding": "none",
        "structure": "single_shot",
        "era": "classical",
    }
    assert axis_coords_key(coords) == "formal|authority|none|single_shot"


def test_enumerate_empty_cells_returns_register_by_framing_product() -> None:
    axis_values = [
        _axis_value("register", "formal"),
        _axis_value("register", "casual"),
        _axis_value("framing", "authority"),
        _axis_value("encoding", "base64"),
        _axis_value("structure", "multi_turn"),
    ]
    cells = enumerate_empty_cells([], axis_values)
    assert cells == [
        {
            "register": "formal",
            "framing": "authority",
            "encoding": "none",
            "structure": "single_shot",
        },
        {
            "register": "casual",
            "framing": "authority",
            "encoding": "none",
            "structure": "single_shot",
        },
    ]


def test_enumerate_empty_cells_skips_missing_register_or_framing_slugs() -> None:
    axis_values = [
        _axis_value("register", "formal"),
        _axis_value("register", ""),
        _axis_value("framing", "authority"),
        _axis_value("framing", " "),
    ]
    cells = enumerate_empty_cells([], axis_values)
    assert cells == [
        {
            "register": "formal",
            "framing": "authority",
            "encoding": "none",
            "structure": "single_shot",
        },
    ]


def test_enumerate_empty_cells_excludes_covered_cells() -> None:
    axis_values = [
        _axis_value("register", "formal"),
        _axis_value("register", "casual"),
        _axis_value("framing", "authority"),
    ]
    arms = [
        {
            "axis_coords": {
                "register": "formal",
                "framing": "authority",
                "encoding": "none",
                "structure": "single_shot",
            }
        }
    ]
    cells = enumerate_empty_cells(arms, axis_values)
    assert cells == [
        {
            "register": "casual",
            "framing": "authority",
            "encoding": "none",
            "structure": "single_shot",
        },
    ]


def test_enumerate_empty_cells_ignores_partial_or_mismatched_axis_coords() -> None:
    axis_values = [
        _axis_value("register", "formal"),
        _axis_value("framing", "authority"),
    ]
    arms = [
        # Partial coords (missing encoding/structure) never block a fully-specified cell.
        {"axis_coords": {"register": "formal", "framing": "authority"}},
        # A different encoding occupies a different product-space point.
        {
            "axis_coords": {
                "register": "formal",
                "framing": "authority",
                "encoding": "base64",
                "structure": "single_shot",
            }
        },
    ]
    cells = enumerate_empty_cells(arms, axis_values)
    assert cells == [
        {
            "register": "formal",
            "framing": "authority",
            "encoding": "none",
            "structure": "single_shot",
        },
    ]
