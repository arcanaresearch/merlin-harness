#!/usr/bin/env python3
"""Run the seeded fixture-target server outside pytest.

The server is the shared target for the headless packaged UAT lanes: it
serves the reviewed ``http_api`` adapter contract under ``/api/`` and the
browser-lane chatbot page at ``/chat``.  On startup it prints one JSON line
with the bound origin and the deterministic ground-truth digests (adapter,
oracle, page) that the campaign driver records before execution.

Run commands::

    uv run python scripts/fixture_target_server.py
    uv run python scripts/fixture_target_server.py --port 8377

Stop with Ctrl-C (SIGINT/SIGTERM).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import signal
from collections.abc import Sequence

from merlin_assessments import FixtureTargetServer


async def _serve(port: int) -> None:
    async with FixtureTargetServer(port=port) as target:
        truth = target.ground_truth()
        print(
            json.dumps({"origin": target.origin, "ground_truth": truth}, sort_keys=True),
            flush=True,
        )
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for signum in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(signum, stop.set)
        await stop.wait()


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=0, help="port to bind (0 = ephemeral)")
    arguments = parser.parse_args(argv)
    asyncio.run(_serve(arguments.port))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
