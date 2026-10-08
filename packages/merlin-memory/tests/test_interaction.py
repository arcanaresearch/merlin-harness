from datetime import UTC, datetime

import pytest
from merlin_memory import InteractionStep, VerifiedInteractionLesson
from pydantic import ValidationError


def lesson(**updates: object) -> VerifiedInteractionLesson:
    values: dict[str, object] = {
        "id": "interaction/plunder/refund-l1/v1",
        "target_id": "https://plunder.me/refund-l1",
        "interface_key": "customer-support-chat",
        "steps": (
            InteractionStep(action="navigate", value="https://plunder.me/refund-l1"),
            InteractionStep(action="fill", locator="textarea", value="{{message}}"),
            InteractionStep(action="press", value="Enter"),
            InteractionStep(action="assert_visible", locator="[data-role=assistant]"),
        ),
        "outcome": "A new assistant message becomes visible after submission.",
        "evidence_references": ("evidence://campaign-1/chat-submit",),
        "verified_by": "validator:browser-outcome",
        "verified_at": datetime(2026, 8, 28, tzinfo=UTC),
        "source_campaign_id": "campaign-1",
        "source_task_id": "task-recon",
    }
    values.update(updates)
    return VerifiedInteractionLesson.model_validate(values)


def test_verified_interaction_is_structured_digestible_and_secret_free() -> None:
    value = lesson()
    assert value.digest.startswith("sha256:")
    assert value.steps[1].value == "{{message}}"


@pytest.mark.parametrize(
    ("field", "value", "match"),
    [
        ("verified_by", "agent:recon", "operator or validator"),
        ("outcome", "token=super-secret-value", "secret-like"),
    ],
)
def test_raw_worker_claims_and_secret_outcomes_are_not_admitted(
    field: str, value: str, match: str
) -> None:
    with pytest.raises(ValidationError, match=match):
        lesson(**{field: value})


def test_fill_requires_runtime_message_placeholder() -> None:
    with pytest.raises(ValidationError, match="placeholder"):
        InteractionStep(action="fill", locator="textarea", value="captured user message")


def test_navigation_rejects_credentials_and_secret_query_parameters() -> None:
    with pytest.raises(ValidationError, match="credentials"):
        InteractionStep(action="navigate", value="https://user:pass@example.test/chat")
    with pytest.raises(ValidationError, match="secret-like"):
        InteractionStep(action="navigate", value="https://example.test/chat?token=abc")
