"""Regression: haiku's prose-wrapped recon result must still yield the interaction lesson.

Live run 13 (campaign 6ee6dcb4) died at wave-1 launch because the recon result
arrived as prose plus a fenced JSON block; a raw ``json.loads`` returned no
lesson and the campaign aborted with "verified reconnaissance guidance is
required before attack" even though the result contained a valid
``interaction_contract``.
"""

import json
from types import SimpleNamespace

from merlin_tui.bootstrap import _verified_interaction_lesson

_CONTRACT = {
    "input_locator": "textarea#userMessage",
    "submit_action": "press Enter",
    "frame_context": "main document",
    "authenticated_session_constraints": "none",
    "input_limit": 4000,
    "probe": "sentinel",
    "lifecycle": {
        "current_state": "ready",
        "terminal_indicator": "reply observed",
        "resume_action": None,
        "rating_action": None,
        "new_session_action": None,
        "cleanup_action": None,
    },
    "success_indicator": "refund policy reply",
    "evidence_refs": ["recon:exchange:1", "recon:exchange:2"],
}


def _manifest() -> SimpleNamespace:
    return SimpleNamespace(target=SimpleNamespace(id="target:plunder", interface="http_api"))


def test_lesson_from_prose_wrapped_recon_result() -> None:
    result_text = (
        "I completed reconnaissance of the target. Here is the structured result:\n\n"
        "```json\n"
        '{"applicable_techniques": [], "inapplicable_techniques": [], '
        + '"interaction_contract": '
        + json.dumps(_CONTRACT)
        + "}\n```\n"
    )
    lesson = _verified_interaction_lesson("campaign-x", _manifest(), "task-1", result_text)
    assert lesson is not None
    assert lesson.target_id == "target:plunder"
    assert lesson.interface_key == "http_api"
    assert lesson.steps[0].action == "fill"
    assert lesson.steps[0].locator == "textarea#userMessage"
    assert lesson.steps[1] == lesson.steps[1].__class__(action="press", value="Enter")
    assert lesson.outcome == "refund policy reply"
    assert lesson.evidence_references == ("recon:exchange:1", "recon:exchange:2")


def test_lesson_from_plain_json_recon_result() -> None:
    result_text = json.dumps(
        {
            "applicable_techniques": [],
            "inapplicable_techniques": [],
            "interaction_contract": {**_CONTRACT, "submit_action": "click #send"},
        }
    )
    lesson = _verified_interaction_lesson("campaign-x", _manifest(), "task-1", result_text)
    assert lesson is not None
    assert lesson.steps[1].action == "click"
    assert lesson.steps[1].locator == "click #send"


def test_lesson_missing_contract_returns_none() -> None:
    lesson = _verified_interaction_lesson(
        "campaign-x", _manifest(), "task-1", '{"applicable_techniques": []}'
    )
    assert lesson is None
