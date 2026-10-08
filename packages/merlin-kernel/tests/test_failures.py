from merlin_contracts import InfrastructureFailureCategory, ToolResult
from merlin_kernel import provider_failure_category, tool_failure_category


def test_provider_failure_categories_distinguish_typed_infrastructure_faults() -> None:
    assert (
        provider_failure_category("read_timeout") == InfrastructureFailureCategory.ADAPTER_TIMEOUT
    )
    assert (
        provider_failure_category("missing_response_pointer")
        == InfrastructureFailureCategory.MISSING_RESPONSE_POINTER
    )
    assert (
        provider_failure_category("target_session_stale")
        == InfrastructureFailureCategory.UNHEALTHY_TARGET_SESSION
    )
    assert (
        provider_failure_category("malformed_stream")
        == InfrastructureFailureCategory.DELIVERY_FAILURE
    )
    assert provider_failure_category("budget_exhausted") is None
    assert provider_failure_category("refusal") is None


def test_tool_failures_require_explicit_typed_metadata() -> None:
    untyped = ToolResult(content="permission denied", is_error=True)
    typed = ToolResult(
        content="adapter timed out",
        is_error=True,
        metadata={
            "infrastructure_failure_category": InfrastructureFailureCategory.ADAPTER_TIMEOUT.value
        },
    )

    assert tool_failure_category(untyped) is None
    assert tool_failure_category(typed) == InfrastructureFailureCategory.ADAPTER_TIMEOUT
