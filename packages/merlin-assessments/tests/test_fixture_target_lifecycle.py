from __future__ import annotations

import asyncio
from contextlib import suppress

import pytest
from merlin_assessments import FixtureTargetServer


@pytest.mark.asyncio
async def test_fixture_close_disconnects_stalled_clients_and_can_restart() -> None:
    target = FixtureTargetServer()
    await target.__aenter__()
    reader, writer = await asyncio.open_connection(
        "127.0.0.1", int(target.origin.rsplit(":", 1)[1])
    )
    writer.write(b"GET /chat HTTP/1.1\r\n")
    await writer.drain()
    for _ in range(100):
        if target._client_tasks:
            break
        await asyncio.sleep(0.01)

    await target.close()

    try:
        disconnected = await asyncio.wait_for(reader.read(), timeout=1)
    except ConnectionResetError:
        disconnected = b""
    assert disconnected == b""
    assert not target._client_tasks
    assert not target._client_writers
    writer.close()
    with suppress(ConnectionResetError):
        await writer.wait_closed()

    async with target:
        restarted_reader, restarted_writer = await asyncio.open_connection(
            "127.0.0.1", int(target.origin.rsplit(":", 1)[1])
        )
        restarted_writer.write(b"GET /chat HTTP/1.1\r\nHost: fixture\r\n\r\n")
        await restarted_writer.drain()
        assert b"200 OK" in await restarted_reader.read()
        restarted_writer.close()
        await restarted_writer.wait_closed()
