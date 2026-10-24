from pathlib import Path

import pytest
from merlin_contracts import InfrastructureFailureCategory, ToolContext, ToolSpec
from merlin_contracts.storage_failures import storage_failure_category
from merlin_tools import ToolBroker
from psycopg.errors import DiskFull


def test_disk_full_is_typed_and_does_not_parse_prose() -> None:
    assert storage_failure_category(DiskFull("localized message")) == (
        InfrastructureFailureCategory.STORAGE_EXHAUSTED
    )
    assert storage_failure_category(RuntimeError("53100 disk full")) is None
    wrapped = RuntimeError("wrapper")
    wrapped.__cause__ = DiskFull()
    assert storage_failure_category(wrapped) == InfrastructureFailureCategory.STORAGE_EXHAUSTED
    wrapped.__cause__ = wrapped
    assert storage_failure_category(wrapped) is None


@pytest.mark.asyncio
async def test_broker_reports_postgres_storage_failure(tmp_path: Path) -> None:
    class Tool:
        effect = "workspace_read"

        def describe(self) -> ToolSpec:
            return ToolSpec(name="read", description="", input_schema={})

        async def invoke(self, context, arguments):
            raise DiskFull("injected")

    broker = ToolBroker((Tool(),))
    result = await broker.invoke(
        "read",
        ToolContext(session_id="one", workspace_root=str(tmp_path)),
        {},
    )
    assert result.is_error
    assert result.metadata["infrastructure_failure_category"] == "storage_exhausted"


@pytest.mark.asyncio
async def test_broker_preserves_unclassified_exceptions(tmp_path: Path) -> None:
    class Tool:
        effect = "workspace_read"

        def describe(self) -> ToolSpec:
            return ToolSpec(name="read", description="", input_schema={})

        async def invoke(self, context, arguments):
            raise RuntimeError("unrelated programming error")

    with pytest.raises(RuntimeError, match="unrelated programming error"):
        await ToolBroker((Tool(),)).invoke(
            "read",
            ToolContext(session_id="one", workspace_root=str(tmp_path)),
            {},
        )
