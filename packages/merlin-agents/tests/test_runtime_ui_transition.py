from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence

from merlin_agents.runtime_ui_transition import (
    normalize_target_ui_transition,
    strict_pre_effect_transition,
)


def _digest(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode()).hexdigest()


def _transition(*, stale: bool = True) -> dict[str, object]:
    return {
        "schema": "merlin.target-ui-transition.v1",
        "material": True,
        "adapter_stale": stale,
        "before_digest": _digest("before"),
        "after_digest": _digest("after"),
        "changed_dimensions": ["controls"],
        "added_controls": [
            {
                "role": "button",
                "name": "Next Level",
                "frame_ref": "frame-1",
                "input_type": "button",
            }
        ],
        "removed_controls": [],
        "controls_truncated": False,
        "evidence_refs": ["browser:before", "browser:after"],
        "reobservation_recommended": True,
    }


def _unchanged_transition() -> dict[str, object]:
    digest = _digest("unchanged")
    transition_digest = (
        "sha256:"
        + hashlib.sha256(
            json.dumps([digest, digest], sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
    )
    return {
        "schema": "merlin.target-ui-transition.v1",
        "material": False,
        "adapter_stale": False,
        "before_digest": digest,
        "after_digest": digest,
        "transition_digest": transition_digest,
        "changed_dimensions": [],
        "url_change": None,
        "title_change": None,
        "frame_change": None,
        "observation_kind_change": None,
        "added_controls": [],
        "removed_controls": [],
        "controls_truncated": False,
        "reviewed_state_changes": [],
        "state_changes_truncated": False,
        "reviewed_control_state_changes": [],
        "added_visible_text": [],
        "evidence_refs": ["browser:unchanged"],
        "reobservation_recommended": False,
    }


def test_normalize_target_ui_transition_accepts_pre_and_post_shapes() -> None:
    pre = normalize_target_ui_transition({"target_ui_transition": _transition()})
    post = normalize_target_ui_transition({"target_ui_transition": _transition(stale=False)})

    assert pre is not None and pre["adapter_stale"] is True
    assert post is not None and post["adapter_stale"] is False
    assert pre["added_controls"] == _transition()["added_controls"]


def test_normalize_target_ui_transition_accepts_only_exact_unchanged_proof() -> None:
    unchanged = _unchanged_transition()

    normalized = normalize_target_ui_transition({"target_ui_transition": unchanged})
    assert normalized is not None
    assert normalized["material"] is False
    assert normalized["before_digest"] == normalized["after_digest"]
    assert normalized["reobservation_recommended"] is False

    invalid = (
        {**unchanged, "adapter_stale": True},
        {**unchanged, "reobservation_recommended": True},
        {**unchanged, "after_digest": _digest("different")},
        {**unchanged, "changed_dimensions": ["controls"]},
        {**unchanged, "added_controls": [{"role": "button", "name": "Next"}]},
        {**unchanged, "removed_controls": [{"role": "textbox", "name": "Message"}]},
        {
            **unchanged,
            "reviewed_state_changes": [
                {"name": "level", "role": "progress_ordinal", "before": 1, "after": 2}
            ],
        },
        {**unchanged, "title_change": {"before": "old", "after": "new"}},
        {**unchanged, "url_change": {"before": "/one", "after": "/two"}},
        {**unchanged, "frame_change": {"added": ["child"], "removed": []}},
        {
            **unchanged,
            "observation_kind_change": {"before": "page", "after": "blocker"},
        },
        {
            **unchanged,
            "reviewed_control_state_changes": [
                {
                    "name": "Message",
                    "before": {"disabled": False},
                    "after": {"disabled": True},
                }
            ],
        },
        {**unchanged, "added_visible_text": ["Level complete"]},
        {**unchanged, "controls_truncated": True},
        {**unchanged, "state_changes_truncated": True},
        {**unchanged, "controls_truncated": 0},
        {**unchanged, "state_changes_truncated": 0},
        {**unchanged, "changed_dimensions": ""},
        {**unchanged, "added_controls": {}},
        {**unchanged, "removed_controls": ()},
        {**unchanged, "reviewed_state_changes": None},
        {**unchanged, "reviewed_control_state_changes": ""},
        {**unchanged, "added_visible_text": {}},
        {**unchanged, "evidence_refs": []},
        {**unchanged, "evidence_refs": ["x" * 257]},
        {**unchanged, "transition_digest": _digest("wrong-transition")},
        {**unchanged, "unreviewed_field": "ignored-before-this-fix"},
    )
    for candidate in invalid:
        assert normalize_target_ui_transition({"target_ui_transition": candidate}) is None


def test_normalize_target_ui_transition_bounds_durable_evidence() -> None:
    transition = _transition()
    transition.update(
        {
            "changed_dimensions": ["x" * 80] * 20,
            "added_controls": [
                {"role": "button", "name": f"Control {index}"} for index in range(25)
            ],
            "removed_controls": [
                {"role": "link", "name": f"Removed {index}"} for index in range(25)
            ],
            "controls_truncated": True,
            "evidence_refs": ["e" * 300] * 12,
            "title_change": {"before": "a" * 600, "after": "b" * 700},
            "reviewed_state_changes": [
                {
                    "name": f"level_{index}",
                    "role": "progress_ordinal",
                    "before": index,
                    "after": index + 1,
                    "boundary": "stage",
                }
                for index in range(40)
            ],
            "state_changes_truncated": True,
        }
    )

    normalized = normalize_target_ui_transition({"target_ui_transition": transition})

    assert normalized is not None
    assert len(normalized["changed_dimensions"]) == 12
    assert all(len(value) == 64 for value in normalized["changed_dimensions"])
    assert len(normalized["added_controls"]) == 20
    assert len(normalized["removed_controls"]) == 20
    assert normalized["controls_truncated"] is True
    assert len(normalized["evidence_refs"]) == 8
    assert all(len(value) == 256 for value in normalized["evidence_refs"])
    assert normalized["title_change"] == {
        "before": "a" * 512,
        "after": "b" * 512,
    }
    assert len(normalized["reviewed_state_changes"]) == 32
    assert normalized["state_changes_truncated"] is True
    assert normalized["reviewed_state_changes"][0] == {
        "name": "level_0",
        "role": "progress_ordinal",
        "before": 0,
        "after": 1,
        "boundary": "stage",
    }


def test_normalize_target_ui_transition_keeps_boundary_after_many_nonboundary_changes() -> None:
    transition = {
        **_transition(),
        "changed_dimensions": ["reviewed_state"],
        "reviewed_state_changes": [
            {
                "name": f"score_{index}",
                "role": "score",
                "before": index,
                "after": index + 1,
            }
            for index in range(20)
        ]
        + [
            {
                "name": "level",
                "role": "progress_ordinal",
                "before": 1,
                "after": 2,
                "boundary": "stage",
            }
        ],
    }

    normalized = normalize_target_ui_transition({"target_ui_transition": transition})

    assert normalized is not None
    assert len(normalized["reviewed_state_changes"]) == 21
    assert normalized["reviewed_state_changes"][-1]["boundary"] == "stage"


def test_normalize_target_ui_transition_rejects_malformed_title_change() -> None:
    transition = {**_transition(), "title_change": {"before": "old"}}

    assert normalize_target_ui_transition({"target_ui_transition": transition}) is None


def test_normalize_target_ui_transition_rejects_malformed_reviewed_state_change() -> None:
    invalid_changes = (
        {
            "name": "level",
            "role": ["progress_ordinal"],
            "before": 1,
            "after": 2,
            "boundary": "stage",
        },
        {
            "name": "score",
            "role": "score",
            "before": 0.0,
            "after": float("inf"),
        },
        {
            "name": "level",
            "role": "progress_ordinal",
            "before": 1,
            "after": 10**128,
            "boundary": "stage",
        },
        {
            "name": "level",
            "role": "progress_ordinal",
            "before": -(10**128),
            "after": 1,
            "boundary": "stage",
        },
        {
            "name": "status",
            "role": "custom",
            "before": "active",
            "after": "x" * 513,
            "boundary": "success",
        },
    )

    for invalid_change in invalid_changes:
        transition = {
            **_transition(),
            "reviewed_state_changes": [invalid_change],
        }

        assert normalize_target_ui_transition({"target_ui_transition": transition}) is None


def test_normalize_target_ui_transition_accepts_maximum_reviewed_state_string() -> None:
    transition = {
        **_transition(),
        "changed_dimensions": ["reviewed_state"],
        "reviewed_state_changes": [
            {
                "name": "status",
                "role": "custom",
                "before": "a" * 512,
                "after": "b" * 512,
                "boundary": "success",
            }
        ],
    }

    normalized = normalize_target_ui_transition({"target_ui_transition": transition})

    assert normalized is not None
    assert normalized["reviewed_state_changes"][0]["after"] == "b" * 512


def test_normalize_target_ui_transition_does_not_materialize_untrusted_sequences() -> None:
    class CountingSequence(Sequence[object]):
        def __init__(self) -> None:
            self.reads = 0

        def __len__(self) -> int:
            return 1_000_000

        def __getitem__(self, index: int) -> object:
            if index >= len(self):
                raise IndexError(index)
            self.reads += 1
            return "controls"

    values = CountingSequence()
    transition = {**_transition(), "changed_dimensions": values}

    normalized = normalize_target_ui_transition({"target_ui_transition": transition})

    assert normalized is not None
    assert normalized["changed_dimensions"] == ["controls"] * 12
    assert values.reads == 12


def test_normalize_target_ui_transition_rejects_unqualified_or_nonmaterial_drift() -> None:
    equal = _transition()
    equal["after_digest"] = equal["before_digest"]
    nonmaterial = {**_transition(), "material": False}

    assert normalize_target_ui_transition({"target_ui_transition": equal}) is None
    assert normalize_target_ui_transition({"target_ui_transition": nonmaterial}) is None
    assert normalize_target_ui_transition({"target_ui_transition": {}}) is None


def test_strict_pre_effect_transition_requires_all_zero_effect_proofs() -> None:
    metadata = {
        "target_effect_attempted": False,
        "target_call_budget_charge": 0,
        "target_ui_transition": _transition(),
    }

    assert (
        strict_pre_effect_transition(
            metadata,
            is_error=True,
            interaction_count=0,
            limit_interaction_count=0,
        )
        is not None
    )
    invalid_cases = (
        ({**metadata, "target_call_budget_charge": 1}, True, 0, 0),
        ({**metadata, "target_call_budget_charge": -1}, True, 0, 0),
        ({**metadata, "target_call_budget_charge": False}, True, 0, 0),
        ({**metadata, "target_effect_attempted": True}, True, 0, 0),
        (metadata, False, 0, 0),
        (metadata, True, 1, 0),
        (metadata, True, 0, 1),
    )
    for candidate, is_error, interactions, limit_interactions in invalid_cases:
        assert (
            strict_pre_effect_transition(
                candidate,
                is_error=is_error,
                interaction_count=interactions,
                limit_interaction_count=limit_interactions,
            )
            is None
        )
