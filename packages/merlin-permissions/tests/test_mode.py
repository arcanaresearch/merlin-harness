import asyncio

import pytest
from merlin_permissions import PermissionEngine, PermissionMode, PermissionService


async def test_mode_waits_for_successful_audit() -> None:
    entered, release = asyncio.Event(), asyncio.Event()

    class Store:
        async def record_audit(self, audit):
            assert audit.request.scope.session_id == "active-session"
            assert audit.mode is PermissionMode.YOLO
            assert engine.mode is PermissionMode.PROMPT
            entered.set()
            await release.wait()

    store = Store()
    engine = PermissionEngine(PermissionMode.PROMPT, store)
    service = PermissionService(engine, store)
    pending = asyncio.create_task(service.set_mode("yolo", session_id="active-session"))
    await asyncio.wait_for(entered.wait(), 1)
    assert await service.get_mode() == "prompt"
    release.set()
    assert await pending == "yolo"


async def test_audit_failure_leaves_mode_unchanged() -> None:
    class Store:
        async def record_audit(self, audit):
            raise OSError("audit unavailable")

    store = Store()
    service = PermissionService(PermissionEngine(PermissionMode.PROMPT, store), store)
    with pytest.raises(OSError, match="audit unavailable"):
        await service.set_mode("yolo", session_id="active-session")
    assert await service.get_mode() == "prompt"


async def test_invalid_mode_does_not_write_audit() -> None:
    class Store:
        async def record_audit(self, audit):
            pytest.fail("invalid mode must not be audited as an allowed change")

    store = Store()
    service = PermissionService(PermissionEngine(PermissionMode.AUTO, store), store)
    with pytest.raises(ValueError):
        await service.set_mode("invalid", session_id="active-session")
    assert await service.get_mode() == "auto"
