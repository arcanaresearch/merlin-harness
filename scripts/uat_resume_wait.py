"""One-off: resume the paused UAT campaign and wait for terminal status.

Reuses the same LocalControlClient the acceptance driver uses. Exits 0 when the
campaign reaches a terminal status; non-zero on timeout or error.
"""

import asyncio
import sys

from merlin_tui.local_control import LocalControlClient

CAMPAIGN_ID = "campaign/09ff0c4b-a48a-47fb-9f6e-2ba280a0d09d"
SESSION_ID = "fc7d1571-3751-4f03-9b8a-54ba920367d6"
TERMINAL = {"completed", "failed", "cancelled", "stopped"}


async def main() -> int:
    client = LocalControlClient()
    if "--no-resume" not in sys.argv:
        async for event in client.stream(f"/resume {SESSION_ID}"):
            if getattr(event, "kind", "") == "error":
                print(f"resume session failed: {event!r}", flush=True)
                return 1
        await client.resume_campaign(CAMPAIGN_ID)
        print(f"resumed {CAMPAIGN_ID}", flush=True)
    deadline = asyncio.get_running_loop().time() + 3 * 3600
    while asyncio.get_running_loop().time() < deadline:
        status = await client.campaign_status(CAMPAIGN_ID)
        data = vars(status) if not isinstance(status, dict) else status
        state = data.get("status")
        running = 0
        for wave in data.get("waves") or []:
            wave_data = vars(wave) if not isinstance(wave, dict) else wave
            for task in wave_data.get("tasks") or []:
                task_data = vars(task) if not isinstance(task, dict) else task
                if task_data.get("state") == "running":
                    running += 1
        print(f"status={state} running={running}", flush=True)
        if state in TERMINAL:
            print(f"TERMINAL status={state} stop_reason={data.get('stop_reason')}", flush=True)
            return 0 if state == "completed" else 2
        await asyncio.sleep(30)
    print("timeout waiting for terminal", flush=True)
    return 3


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
