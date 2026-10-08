#!/usr/bin/env python3
"""Operator helper: open the Merlin persistent browser profile WITHOUT target-scope
route enforcement so the operator can sign in to an authenticated target (Google OAuth
or any identity provider). The daemon's normal browser bounds would abort any request to
the identity-provider origin (ERR_BLOCKED_BY_CLIENT), which is correct for recon but
wrong for the operator's one-time authentication step.

Usage: run with the daemon STOPPED (this script needs the profile lock). It opens a
visible window on the target, then polls the target's start endpoint until it stops
returning 401, printing AUTHENTICATED and exiting. The operator just signs in once.
"""

from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path

from playwright.async_api import async_playwright

PROFILE = Path(
    os.environ.get("MERLIN_BROWSER_PROFILE", "/Users/lucas/.merlin-home/.playwright-profile")
)
TARGET = os.environ.get("MERLIN_LOGIN_TARGET", "https://plunder.me")
START_URL = os.environ.get(
    "MERLIN_LOGIN_START_URL", "https://plunder.me/api/policy-refund-trap/start"
)
START_BODY = os.environ.get("MERLIN_LOGIN_START_BODY", '{"aiAssisted":true,"levelId":1}')
TIMEOUT_SECONDS = int(os.environ.get("MERLIN_LOGIN_TIMEOUT", "600"))


async def main() -> None:
    async with async_playwright() as p:
        context = await p.chromium.launch_persistent_context(str(PROFILE), headless=False)
        page = context.pages[0] if context.pages else await context.new_page()
        await page.goto(TARGET)
        print(f"OPEN {TARGET} — sign in, then this script auto-detects success.", flush=True)
        deadline = time.monotonic() + TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            try:
                response = await context.request.post(
                    START_URL,
                    headers={"Content-Type": "application/json"},
                    data=START_BODY,
                )
                if response.status != 401:
                    print(f"AUTHENTICATED status={response.status}", flush=True)
                    return
            except Exception as error:  # noqa: BLE001 - keep polling through transient errors
                print(f"probe error: {error}", flush=True)
            await asyncio.sleep(5)
        print("TIMEOUT waiting for sign-in", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
