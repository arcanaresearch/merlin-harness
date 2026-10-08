"""Four-axis arm taxonomy helpers.

Ported from pirateboys-scanner-v1's discovery layer: axis values parameterize
the register x framing x encoding x structure product space, and empty-cell
enumeration drives gap-fill proposal. Pure functions only — no provider or
database.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

AXES = ("register", "framing", "encoding", "structure")

_CELL_ENCODING = "none"
_CELL_STRUCTURE = "single_shot"


def axis_coords_key(coords: Mapping[str, object] | None) -> str | None:
    """Return ``"register|framing|encoding|structure"`` or ``None`` when incomplete.

    Any missing, empty, or whitespace-only member makes the coordinates
    incomplete, so a partially-specified arm (for example a win-generalization
    arm that pins only its framing) never collides with a fully-specified cell.
    """
    if not isinstance(coords, Mapping):
        return None
    parts: list[str] = []
    for axis in AXES:
        value = coords.get(axis)
        if value is None:
            return None
        text = str(value).strip()
        if not text:
            return None
        parts.append(text)
    return "|".join(parts)


def enumerate_empty_cells(
    arms: Sequence[Mapping[str, object]],
    axis_values: Sequence[Mapping[str, object]],
) -> list[dict[str, object]]:
    """Return register x framing cells no existing arm's ``axis_coords`` covers.

    Encoding and structure are pinned to ``"none"`` and ``"single_shot"``
    (mirroring gap-fill.ts), so only the register and framing vocabularies are
    enumerated. Axis values whose slug is missing are skipped, and an arm only
    covers a cell when its coordinates fully specify all four axes.
    """
    registers = _axis_slugs(axis_values, "register")
    framings = _axis_slugs(axis_values, "framing")
    occupied: set[str] = set()
    for arm in arms:
        if not isinstance(arm, Mapping):
            continue
        key = axis_coords_key(arm.get("axis_coords"))
        if key is not None:
            occupied.add(key)
    cells: list[dict[str, object]] = []
    for register in registers:
        for framing in framings:
            cell: dict[str, object] = {
                "register": register,
                "framing": framing,
                "encoding": _CELL_ENCODING,
                "structure": _CELL_STRUCTURE,
            }
            if axis_coords_key(cell) not in occupied:
                cells.append(cell)
    return cells


def _axis_slugs(axis_values: Sequence[Mapping[str, object]], axis: str) -> list[str]:
    slugs: list[str] = []
    for value in axis_values:
        if not isinstance(value, Mapping):
            continue
        if value.get("axis") != axis:
            continue
        slug = str(value.get("slug") or "").strip()
        if slug:
            slugs.append(slug)
    return slugs
