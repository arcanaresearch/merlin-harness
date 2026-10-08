from __future__ import annotations

from merlin_contracts import InfrastructureFailureCategory, ToolResult

_NON_INFRASTRUCTURE_PROVIDER_CODES = frozenset({"budget_exhausted", "refusal", "truncated"})


def provider_failure_category(code: str) -> InfrastructureFailureCategory | None:
    """Map provider transport codes onto the campaign's typed failure taxonomy."""
    normalized = code.strip().lower()
    if normalized in _NON_INFRASTRUCTURE_PROVIDER_CODES:
        return None
    if "pointer" in normalized and ("missing" in normalized or "response" in normalized):
        return InfrastructureFailureCategory.MISSING_RESPONSE_POINTER
    if "session" in normalized and any(
        marker in normalized for marker in ("closed", "expired", "stale", "unhealthy")
    ):
        return InfrastructureFailureCategory.UNHEALTHY_TARGET_SESSION
    if "timeout" in normalized:
        return InfrastructureFailureCategory.ADAPTER_TIMEOUT
    return InfrastructureFailureCategory.DELIVERY_FAILURE


def tool_failure_category(result: ToolResult) -> InfrastructureFailureCategory | None:
    """Read an explicitly typed tool failure without guessing from human prose."""
    if not result.is_error:
        return None
    value = result.metadata.get("infrastructure_failure_category")
    if isinstance(value, InfrastructureFailureCategory):
        return value
    if isinstance(value, str):
        try:
            return InfrastructureFailureCategory(value)
        except ValueError:
            return None
    return None


def failure_event_payload(
    category: InfrastructureFailureCategory | None,
) -> dict[str, str]:
    """Return the stable event delta used by campaign observability consumers."""
    if category is None:
        return {}
    return {
        "failure_class": "infrastructure",
        "infrastructure_failure_category": category.value,
    }
